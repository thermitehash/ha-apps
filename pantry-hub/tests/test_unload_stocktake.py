"""v0.4.3: Unload (groceries bought elsewhere) and Stock-take."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from pantry_hub.store import Link

from .conftest import BrokerHandle, FakeClock, eventually
from .fake_grocy import Entry, FakeGrocy
from .fake_ha import TOKEN, FakeHomeAssistant
from .fake_kroger import FakeKroger, item
from .rig import Observer, Rig
from .web_client import WebClient

STATION = "kroger_scanner"
SCAN = f"pantry_hub/linking/{STATION}/scan"
MODE_STATE = f"pantry_hub/linking/{STATION}/mode_state"
MODE = f"pantry_hub/linking/{STATION}/mode"
RESULT = f"pantry_hub/linking/{STATION}/result"
TAG = f"pantry_hub/linking/{STATION}/tag"
START = f"pantry_hub/linking/{STATION}/start"
COKE = "049000028911"
EPCS = [f"E2801170000003{i:010X}" for i in range(1, 9)]


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat().replace("+00:00", "Z")


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


async def start(rig: Rig, ha: FakeHomeAssistant, **kw: Any) -> None:
    kw.setdefault("counted_idle", 1.0)
    await rig.start(
        ha_url=ha.url,
        ha_token=TOKEN,
        picture_retry_delays=(),
        picture_spacing_s=0.0,
        ha_backoff_s=0.05,
        **kw,
    )
    assert rig.h.ha_events is not None
    await asyncio.wait_for(rig.h.ha_events.subscribed.wait(), 5)


def results(observer: Observer, n: int) -> Callable[[], bool]:
    """Condition: the station has received ``n`` results."""
    return lambda: len(observer.json(RESULT)) == n


async def unload_rows(rig: Rig) -> list[dict[str, Any]]:
    assert rig.h.just_bought is not None
    return [r for r in await rig.h.just_bought.rows() if r["source"] == "unload"]


def stock(grocy: FakeGrocy, product_id: int, amount: float, location: int = 1) -> None:
    grocy.entries.append(
        Entry(
            stock_id=f"seed{len(grocy.entries)}",
            product_id=product_id,
            amount=amount,
            location_id=location,
            note="seeded",
            best_before_date=None,
        )
    )


# Unload --------------------------------------------------------------------------


async def test_unload_scans_find_or_create_products_and_count(
    rig: Rig,
    ha: FakeHomeAssistant,
    grocy: FakeGrocy,
    observer: Observer,
) -> None:
    coke = grocy.add_product("Coke", ["0049000028911"])  # stored as EAN-13
    kroger = FakeKroger(ha)
    kroger.add("011110428509", name="Kroger Black Beans", brand="Kroger", size="15 oz")
    grocy.lookup["5000112637922"] = "Plugin Biscuits"
    await start(rig, ha)
    await observer.publish(MODE_STATE, {"mode": "unload"})
    await eventually(
        lambda: rig.h.scans is not None and rig.h.scans.session is not None
    )
    for code in (COKE, COKE, "011110428509", "5000112637922", "99999999"):
        await observer.publish(SCAN, {"barcode": code, "mode": "unload", "ts": 1})
    await eventually(lambda: len(observer.json(RESULT)) == 5)
    statuses = [r["status"] for r in observer.json(RESULT)]
    assert statuses == ["added", "added", "added", "added", "unknown_barcode"]
    assert observer.json(RESULT)[1]["message"] == "Just bought: Coke ×2"
    rows = {r["name"]: r for r in await unload_rows(rig)}
    assert rows["Coke"]["quantity"] == 2 and rows["Coke"]["product_id"] == coke.id
    assert rows["Kroger Black Beans"]["quantity"] == 1
    assert rows["Plugin Biscuits"]["quantity"] == 1
    beans = [p for p in grocy.products.values() if p.name == "Kroger Black Beans"]
    assert beans[0].barcodes == ["011110428509"]
    assert beans[0].description == "Kroger · 15 oz"
    assert rig.store is not None
    assert [n.barcode for n in await rig.store.needs_product()] == ["99999999"]
    # Never the Kroger cart: no scan_upc, by REST or websocket.
    assert ha.rest("kroger_restock", "scan_upc") == []
    assert ha.calls("kroger_restock", "scan_upc") == []
    assert all(sv != "scan_upc" for _, sv, _ in ha.rest_calls)
    # Grocy stock is untouched until Tag them / Not tagging.
    assert grocy.count_calls("POST", "/add") == 0


async def test_unload_rows_not_tagging_books_without_price(
    rig: Rig, ha: FakeHomeAssistant, grocy: FakeGrocy
) -> None:
    coke = grocy.add_product("Coke", [COKE])
    await start(rig, ha)
    async with WebClient(rig.h) as c:
        assert (await c.post("/scan/start", mode="unload"))["ok"]
        for _ in range(3):
            assert (await c.post("/scan", barcode=COKE))["ok"]
        (row,) = await unload_rows(rig)
        assert row["quantity"] == 3 and row["to_tag"] == 3
        reply = await c.post("/bought/skip", row=row["id"], add_stock="on")
        assert reply["message"] == "Added 3 × Coke to Grocy stock"
    adds = [b for m, p, b in grocy.calls if m == "POST" and p.endswith("/add")]
    assert adds == [
        {
            "amount": 3,
            "transaction_type": "purchase",
            "location_id": 1,
            "note": "Pantry Hub: unloaded groceries",
        }
    ]
    assert grocy.amount(coke.id) == 3


async def test_unload_row_tag_them_counted_session(
    rig: Rig, ha: FakeHomeAssistant, grocy: FakeGrocy, observer: Observer
) -> None:
    grocy.add_product("Coke", [COKE])
    await start(rig, ha)
    async with WebClient(rig.h) as c:
        await c.post("/scan", barcode=COKE, mode="unload")
        await c.post("/scan", barcode=COKE, mode="unload")
        (row,) = await unload_rows(rig)
        assert (await c.post("/bought/tag", row=row["id"]))["ok"]
    await eventually(lambda: observer.last(START) is not None)
    assert observer.last(START) == {"barcode": COKE, "count": 2}
    await observer.publish(TAG, {"epc": EPCS[0], "rssi": -40})
    await eventually(lambda: observer.last(RESULT) is not None)
    assert observer.last(RESULT)["remaining"] == 1


async def _untracked(rig: Rig, remaining: int | None) -> bool:
    assert rig.h.just_bought is not None
    u = await rig.h.just_bought.untracked()
    return (
        (u is None)
        if remaining is None
        else (u is not None and u["remaining"] == remaining)
    )


async def test_untracked_event_countdown_with_order_link(
    rig: Rig, ha: FakeHomeAssistant, grocy: FakeGrocy, clock: FakeClock
) -> None:
    grocy.add_product("Coke", [COKE])
    grocy.add_product("Beans", ["011110428509"])
    kroger = FakeKroger(ha)
    # A tracked item of the same order, checked off by hand in Kroger Restock.
    kroger.recent = [
        item(
            item_id="it-1",
            barcode="011110428509",
            received_at=iso(clock() - 700),
            order_ref="ORD-9",
        )
    ]
    await start(rig, ha)
    await eventually(lambda: _untracked(rig, None))
    order = kroger.untracked_order("ORD-9", 3, iso(clock() - 600))
    await kroger.fire_untracked(order)
    await eventually(lambda: _untracked(rig, 3))
    async with WebClient(rig.h) as c:
        u = (await c.state())["just_bought"]["untracked"]
        assert u["order_ref"] == "ORD-9" and u["count"] == 3
        assert u["pickup_window"] == "Fri 4:00–5:00 PM"
        # Found by its order_ref, not a hard-coded entity id.
        assert (u["item_count"], u["tracked_units"]) == (40, 37)
        await c.post("/scan", barcode=COKE, mode="unload")
        await c.post("/scan", barcode=COKE, mode="unload")
        assert (await c.state())["just_bought"]["untracked"]["remaining"] == 1
        # A product Kroger Restock tracked in this order isn't untracked.
        await c.post("/scan", barcode="011110428509", mode="unload")
        assert (await c.state())["just_bought"]["untracked"]["remaining"] == 1
        # Stock-take scans don't count down.
        await c.post("/scan", barcode=COKE, mode="stocktake")
        assert (await c.state())["just_bought"]["untracked"]["remaining"] == 1
        # The last one: the prompt goes; extra scans are still taken.
        await c.post("/scan", barcode=COKE, mode="unload")
        assert (await c.state())["just_bought"]["untracked"] is None
        reply = await c.post("/scan", barcode=COKE, mode="unload")
        assert reply["ok"] and reply["message"] == "Just bought: Coke ×4"
    rows = {(r["name"], r["quantity"]) for r in await unload_rows(rig)}
    assert rows == {("Coke", 4), ("Beans", 1)}
    assert rig.store is not None
    assert await rig.store.unload_scans("ORD-9") == 4  # Coke rows tied to it
    # The event repeated (or reported again by the catch-up): counted once.
    await kroger.fire_untracked(order)
    await asyncio.sleep(0.1)
    assert await _untracked(rig, None)
    # More than 6 hours after pickup a new order's scans don't reach it.
    clock.advance(6 * 3600)
    assert await rig.h.just_bought.unload_order(clock(), COKE) is None  # type: ignore[union-attr]


async def test_untracked_caught_up_after_restart(
    tmp_path: Path,
    grocy: FakeGrocy,
    broker: BrokerHandle,
    clock: FakeClock,
    ha: FakeHomeAssistant,
) -> None:
    """A pickup while Pantry Hub was down still shows the prompt."""
    kroger = FakeKroger(ha)
    kroger.untracked_order("ORD-7", 12, iso(clock() - 1800), sensor=None)
    r = Rig(tmp_path, grocy, broker, clock)
    try:
        await start(r, ha)
        await eventually(lambda: _untracked(r, 12))
        async with WebClient(r.h) as c:
            u = (await c.state())["just_bought"]["untracked"]
        assert u["order_ref"] == "ORD-7" and u["item_count"] is None
    finally:
        await r.stop()
        r.close()


async def test_older_kroger_restock_no_untracked_prompt(
    rig: Rig,
    ha: FakeHomeAssistant,
    grocy: FakeGrocy,
    clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    grocy.add_product("Coke", [COKE])
    kroger = FakeKroger(ha, version="0.17.2")  # no untracked_orders field
    kroger.recent = [item(item_id="it-1", barcode=COKE, received_at=iso(clock()))]
    await start(rig, ha)
    await eventually(lambda: len(ha.rest("kroger_restock", "recent_received")) == 1)
    async with WebClient(rig.h) as c:
        assert (await c.post("/scan", barcode=COKE, mode="unload"))["ok"]
        assert (await c.state())["just_bought"]["untracked"] is None
    # Odd events (count 0, missing fields) are ignored quietly.
    for bad in ({"order_ref": "X", "untracked_count": 0}, {"untracked_count": 3}, {}):
        await ha.fire_event("kroger_restock_untracked_items", bad)
    await asyncio.sleep(0.1)
    assert await _untracked(rig, None)
    assert not [
        r for r in caplog.records if r.levelno >= 30 and "untracked" in r.getMessage()
    ]


async def test_session_idle_timeout_and_station_sync(
    rig: Rig, ha: FakeHomeAssistant, grocy: FakeGrocy, observer: Observer
) -> None:
    grocy.add_product("Coke", [COKE])
    await start(rig, ha, scan_idle_s=0.4)
    scans = rig.h.scans
    assert scans is not None
    async with WebClient(rig.h) as c:
        reply = await c.post("/scan/start", mode="unload")
        assert reply["message"] == f"Unloading on the {STATION} station: scan each item"
        await eventually(lambda: observer.last(MODE) == {"mode": "unload"})
        state = await c.state()
        assert state["scan"]["session"]["mode"] == "unload"
        # 3 minutes (here 0.4 s) without a scan: the session ends, the
        # station is told to go back to Kroger mode.
        await eventually(lambda: scans.session is None, timeout=3)
        await eventually(lambda: observer.last(MODE) == {"mode": "off"})
        assert (await c.state())["scan"]["session"] is None
    # The station enters Unload itself (triple-click) and leaves it (click):
    # the hub follows without echoing commands back.
    n = len(observer.json(MODE))
    await observer.publish(MODE_STATE, {"mode": "unload"})
    await eventually(lambda: scans.session is not None)
    assert scans.session is not None and scans.session.source == STATION
    await observer.publish(MODE_STATE, {"mode": "kroger"})
    await eventually(lambda: scans.session is None)
    await asyncio.sleep(0.1)
    assert len(observer.json(MODE)) == n


async def test_scan_rejects_garbage(rig: Rig, ha: FakeHomeAssistant) -> None:
    await start(rig, ha)
    async with WebClient(rig.h) as c:
        reply = await c.post("/scan", barcode="hello", mode="unload")
        assert reply["ok"] is False and "isn't a product barcode" in reply["message"]
        reply = await c.post("/scan", barcode="0001", mode="shopping")
        assert reply["ok"] is False
        reply = await c.post("/scan/start", mode="dance")
        assert reply["ok"] is False


# Stock-take ----------------------------------------------------------------------


async def seed_stocktake(rig: Rig, grocy: FakeGrocy) -> dict[str, Any]:
    """A: Grocy 2, counted 3. B: pantry 5 + fridge 1, counted 2.
    C: Grocy 4, not counted. D: 2 tags present, Grocy 2, not counted.
    E: 1 tag present, counted 1 untagged, Grocy 1.
    """
    a = grocy.add_product("Apples", ["1001"])
    b = grocy.add_product("Beans", ["1002"])
    c = grocy.add_product("Corn", ["1003"])
    d = grocy.add_product("Dates", ["1004"])
    e = grocy.add_product("Eggs", ["1005"])
    stock(grocy, a.id, 2)
    stock(grocy, b.id, 5)
    stock(grocy, b.id, 1, location=2)
    stock(grocy, c.id, 4)
    stock(grocy, d.id, 2)
    stock(grocy, e.id, 1)
    assert rig.store is not None
    for epc, p in ((EPCS[0], d), (EPCS[1], d), (EPCS[2], e)):
        await rig.store.create_link(
            Link(epc, p.id, p.name, rig.clock()), location_id=1, in_stock=True
        )
    await rig.arrive(EPCS[0], EPCS[1], EPCS[2])
    await rig.idle()
    return {"a": a, "b": b, "c": c, "d": d, "e": e}


async def count(c: WebClient, barcode: str, n: int) -> None:
    for _ in range(n):
        reply = await c.post("/scan", barcode=barcode, mode="stocktake")
        assert reply["ok"], reply


async def test_stocktake_review_math(
    rig: Rig, ha: FakeHomeAssistant, grocy: FakeGrocy
) -> None:
    await start(rig, ha)
    p = await seed_stocktake(rig, grocy)
    async with WebClient(rig.h) as c:
        assert (await c.post("/scan/start", mode="stocktake"))["ok"]
        await count(c, "1001", 3)
        await count(c, "1002", 1)
        # Typed quantity instead of scanning every unit.
        assert (await c.post("/stocktake/count", product_id=p["b"].id, counted=2))["ok"]
        await count(c, "1005", 1)
        counting = (await c.state())["stocktake"]
        assert counting["phase"] == "counting"
        assert {r["product_name"]: r["scanned"] for r in counting["counts"]} == {
            "Apples": 3,
            "Beans": 2,
            "Eggs": 1,
        }
        assert (await c.post("/stocktake/finish"))["message"] == "Review 4 products"
        review = {r["name"]: r for r in (await c.state())["stocktake"]["review"]}
    assert set(review) == {"Apples", "Beans", "Eggs", "Corn"}  # Dates: RFID owns it
    a, b, e, corn = review["Apples"], review["Beans"], review["Eggs"], review["Corn"]
    assert (a["counted"], a["tags"], a["target"], a["grocy"], a["difference"]) == (
        3,
        0,
        3,
        2,
        1,
    )
    # Beans: Grocy's pantry amount only (the fridge unit isn't counted here).
    assert (b["target"], b["grocy"], b["difference"]) == (2, 5, -3)
    assert (e["counted"], e["tags"], e["target"], e["difference"]) == (1, 1, 2, 1)
    assert (corn["scanned"], corn["counted"], corn["grocy"]) == (False, 0, 4)
    assert rig.h.scans is not None and rig.h.scans.session is None  # ended


async def test_stocktake_set_uses_inventory_and_leaves_unscanned_alone(
    rig: Rig, ha: FakeHomeAssistant, grocy: FakeGrocy
) -> None:
    await start(rig, ha)
    p = await seed_stocktake(rig, grocy)
    async with WebClient(rig.h) as c:
        await count(c, "1001", 3)
        await count(c, "1002", 2)
        await c.post("/stocktake/finish")
        n_add = grocy.count_calls("POST", "/add")
        reply = await c.post("/stocktake/apply", how="set")
        assert reply["message"] == "Set 2 products in Grocy"
        state = (await c.state())["stocktake"]
    assert state["phase"] == "applied"
    # Inventory corrections, not purchases: no /add calls, prices untouched.
    assert grocy.count_calls("POST", "/add") == n_add
    # Raising (Apples 2 → 3): an inventory correction.
    inv = [(path, body) for m, path, body in grocy.calls if path.endswith("/inventory")]
    assert inv == [
        (
            f"/api/stock/products/{p['a'].id}/inventory",
            {"new_amount": 3.0, "location_id": 1, "note": "Pantry Hub stock-take"},
        )
    ]
    # Lowering (Beans 5 → 2): consumed from the Pantry only (0.4.4).
    eaten = [
        (path, body)
        for m, path, body in grocy.calls
        if path.endswith("/consume") and body.get("note") == "Pantry Hub stock-take"
    ]
    assert eaten == [
        (
            f"/api/stock/products/{p['b'].id}/consume",
            {
                "amount": 3,
                "transaction_type": "consume",
                "spoiled": False,
                "location_id": 1,
                "note": "Pantry Hub stock-take",
            },
        )
    ]
    assert grocy.amount(p["a"].id) == 3
    assert grocy.amount(p["b"].id) == 2
    assert grocy.amount(p["b"].id, location_id=2) == 1  # fridge kept
    assert grocy.amount(p["c"].id) == 4  # not scanned: untouched by default
    assert rig.store is not None
    assert await rig.store.allowance_units(rig.clock()) == {p["a"].id: 3, p["b"].id: 2}


async def test_stocktake_purchase_and_gone(
    rig: Rig, ha: FakeHomeAssistant, grocy: FakeGrocy
) -> None:
    await start(rig, ha)
    p = await seed_stocktake(rig, grocy)
    async with WebClient(rig.h) as c:
        await count(c, "1001", 3)
        await count(c, "1002", 2)
        await c.post("/stocktake/finish")
        reply = await c.post("/stocktake/apply", how="purchase", gone="on")
        assert reply["ok"], reply
        review = {r["name"]: r for r in (await c.state())["stocktake"]["review"]}
    adds = [
        (path, body)
        for m, path, body in grocy.calls
        if m == "POST" and path.endswith("/add")
    ]
    # Only the missing apple is bought (no price, stock-take note); beans'
    # surplus isn't consumed by "Add as purchases".
    assert adds == [
        (
            f"/api/stock/products/{p['a'].id}/add",
            {
                "amount": 1,
                "transaction_type": "purchase",
                "location_id": 1,
                "note": "Pantry Hub stock-take",
            },
        )
    ]
    assert grocy.amount(p["a"].id) == 3
    assert grocy.amount(p["b"].id) == 5
    # "Products I didn't scan are gone": Corn → 0 in the pantry.
    assert grocy.amount(p["c"].id) == 0
    assert review["Corn"]["applied"] == "gone"
    assert review["Beans"]["applied"] is None


async def test_tag_from_stocktake_no_double_count(
    rig: Rig, ha: FakeHomeAssistant, grocy: FakeGrocy, observer: Observer
) -> None:
    await start(rig, ha)
    p = await seed_stocktake(rig, grocy)
    async with WebClient(rig.h) as c:
        await count(c, "1001", 3)
        await c.post("/stocktake/finish")
        early = await c.post("/stocktake/tag", product_id=p["a"].id)
        assert early["ok"] is False and "Apply" in early["message"]
        await c.post("/stocktake/apply", how="set")
        assert grocy.amount(p["a"].id) == 3
        reply = await c.post("/stocktake/tag", product_id=p["a"].id)
        assert (
            reply["message"] == f"Tap each Apples on the {STATION} station (3 to tag)"
        )
        await eventually(
            lambda: observer.last(START) == {"barcode": "1001", "count": 3}
        )
        for i, epc in enumerate(EPCS[3:6], start=1):
            await observer.publish(TAG, {"epc": epc, "rssi": -40})
            await eventually(results(observer, i))
        assert observer.last(RESULT)["remaining"] == 0
        review = {r["name"]: r for r in (await c.state())["stocktake"]["review"]}
        assert review["Apples"]["to_tag"] == 0
    # The reader sees the three tagged apples for the first time: matched
    # against the counted units, Grocy stays at 3.
    await rig.arrive(*EPCS[3:6])
    await rig.idle()
    assert grocy.amount(p["a"].id) == 3
    assert rig.store is not None
    assert await rig.store.allowance_units(rig.clock()) == {}


async def test_grocy_only_stock_listed_as_untagged_rows(
    rig: Rig, ha: FakeHomeAssistant, grocy: FakeGrocy, observer: Observer
) -> None:
    await start(rig, ha)
    flour = grocy.add_product("Flour", ["2001"])
    salt = grocy.add_product("Salt", ["2002"])
    stock(grocy, flour.id, 3)
    stock(grocy, salt.id, 1)
    stock(grocy, salt.id, 4, location=2)  # fridge: not pantry
    assert rig.store is not None
    await rig.store.create_link(
        Link(EPCS[0], flour.id, "Flour", rig.clock()), location_id=1, in_stock=True
    )
    await rig.arrive(EPCS[0])
    await rig.idle()
    gu = rig.h.grocy_untagged
    assert gu is not None
    await gu.refresh()
    async with WebClient(rig.h) as c:
        rows = (await c.state())["grocy_untagged"]["rows"]
        assert [
            (r["name"], r["in_grocy"], r["tagged"], r["untagged"]) for r in rows
        ] == [
            ("Flour", 3, 1, 2),
            ("Salt", 1, 0, 1),
        ]
        reply = await c.post("/grocy-stock/tag", product_id=flour.id)
        assert reply["message"] == f"Tap each Flour on the {STATION} station (2 to tag)"
    await eventually(lambda: observer.last(START) == {"barcode": "2001", "count": 2})
    for i, epc in enumerate(EPCS[1:3], start=1):
        await observer.publish(TAG, {"epc": epc, "rssi": -40})
        await eventually(results(observer, i))
    # Tagging existing Grocy units never adds them: arrivals are skipped.
    await rig.arrive(*EPCS[1:3])
    await rig.idle()
    assert grocy.amount(flour.id) == 3
    await gu.refresh()
    assert [r["name"] for r in gu.rows] == ["Salt"]
