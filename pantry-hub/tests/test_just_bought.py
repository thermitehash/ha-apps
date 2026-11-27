"""v0.4.1: "Just bought" from Kroger Restock + counted link sessions."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from pantry_hub.just_bought import OLDER_AFTER_S, PURGE_AFTER_S, Item
from pantry_hub.linking import BOOKED_MATCH_S
from pantry_hub.store import Link

from .conftest import BrokerHandle, FakeClock, eventually
from .fake_grocy import FakeGrocy
from .fake_ha import TOKEN, FakeHomeAssistant
from .rig import Observer, Rig
from .web_client import WebClient

STATION = "kroger_scanner"
RESULT = f"pantry_hub/linking/{STATION}/result"
START = f"pantry_hub/linking/{STATION}/start"
TAG = f"pantry_hub/linking/{STATION}/tag"
EPCS = [f"E2801170000002{i:010X}" for i in range(1, 7)]


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat().replace("+00:00", "Z")


def payload(clock: FakeClock, **over: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "item_id": "it-1",
        "upc": "0001111042850",
        "product_id": "0001111042850",
        "name": "Kroger Black Beans",
        "brand": "Kroger",
        "size": "15 oz",
        "image_url": None,
        "quantity_received": 3,
        "price": 0.89,
        "received_at": iso(clock()),
        "order_ref": "ORD-1",
    }
    data.update(over)
    return data


class Kroger:
    """Kroger Restock's actions on the fake Home Assistant."""

    def __init__(self, ha: FakeHomeAssistant) -> None:
        self.recent: list[dict[str, Any]] = []
        self.lookup: dict[str, dict[str, Any]] = {}
        ha.add_action("kroger_restock", "recent_received", self._recent)
        ha.add_action("kroger_restock", "lookup_product", self._lookup)

    async def _recent(self, data: dict[str, Any]) -> dict[str, Any]:
        return {"items": list(self.recent)}

    async def _lookup(self, data: dict[str, Any]) -> dict[str, Any]:
        return self.lookup.get(str(data["barcode"]), {"found": False})


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


async def rows(rig: Rig) -> list[dict[str, Any]]:
    assert rig.h.just_bought is not None
    return await rig.h.just_bought.rows()


async def tap(observer: Observer, *epcs: str) -> None:
    for epc in epcs:
        await observer.publish(TAG, {"epc": epc, "rssi": -40})


# Intake --------------------------------------------------------------------------


def test_item_parse_rejects_bad_payloads() -> None:
    clock = FakeClock()
    assert Item.parse(payload(clock)) is not None
    for bad in (
        {"item_id": None},
        {"upc": ""},
        {"quantity_received": 0},
        {"quantity_received": "x"},
        {"received_at": None},
    ):
        assert Item.parse(payload(clock, **bad)) is None, bad
    capped = Item.parse(payload(clock, quantity_received=500, name=None))
    assert capped is not None and capped.quantity == 99
    assert capped.name == "0001111042850"


async def test_event_and_catch_up_deduped(
    rig: Rig, ha: FakeHomeAssistant, clock: FakeClock
) -> None:
    kroger = Kroger(ha)
    kroger.recent = [
        payload(clock),
        payload(clock, item_id="it-2", name="Oats", upc="0002", quantity_received=1),
    ]
    await start(rig, ha)
    # Caught up at start.
    await eventually(lambda: len(ha.rest("kroger_restock", "recent_received")) == 1)
    assert [r["name"] for r in await rows(rig)] == ["Oats", "Kroger Black Beans"]
    # The same item as a live event: no duplicate. A new one: added.
    await ha.fire_event("kroger_restock_received", payload(clock))
    new = payload(clock, item_id="it-3", name="Rice", upc="0003")
    await ha.fire_event("kroger_restock_received", new)
    await eventually(lambda: has_rows(rig, 3))
    # Same item_id, another delivery (received_at differs): a new row.
    clock.advance(60)
    await ha.fire_event("kroger_restock_received", payload(clock))
    await eventually(lambda: has_rows(rig, 4))
    # Reconnect → catch up again, still deduped.
    await ha.drop()
    await eventually(lambda: len(ha.rest("kroger_restock", "recent_received")) == 2)
    assert rig.h.ha_events is not None
    await asyncio.wait_for(rig.h.ha_events.subscribed.wait(), 5)
    assert len(await rows(rig)) == 4
    # Malformed events are ignored.
    await ha.fire_event("kroger_restock_received", {"item_id": "x"})
    await asyncio.sleep(0.05)
    assert len(await rows(rig)) == 4
    async with WebClient(rig.h) as c:
        state = await c.state()
    jb = state["just_bought"]
    assert jb["available"] is True
    assert jb["to_tag"] == 3 + 1 + 3 + 3
    assert jb["station"] == STATION
    first = jb["rows"][0]
    assert first["size"] == "15 oz" and first["to_tag"] == 3


async def has_rows(rig: Rig, n: int) -> bool:
    return len(await rows(rig)) == n


async def test_missing_kroger_hides_card(rig: Rig, ha: FakeHomeAssistant) -> None:
    await start(rig, ha)
    assert rig.h.just_bought is not None
    await eventually(
        lambda: rig.h.just_bought is not None and rig.h.just_bought.available is False
    )
    assert ha.rest_calls == []
    async with WebClient(rig.h) as c:
        state = await c.state()
        assert state["just_bought"]["available"] is False
        reply = await c.post("/bought/tag", row=1)
        assert reply["ok"] is False


async def test_expiry_older_then_deleted(
    rig: Rig, ha: FakeHomeAssistant, clock: FakeClock
) -> None:
    Kroger(ha)
    await start(rig, ha)
    jb = rig.h.just_bought
    assert jb is not None
    assert await jb.ingest(payload(clock, received_at=iso(clock() - OLDER_AFTER_S - 5)))
    assert await jb.ingest(payload(clock, item_id="new"))
    # Already past 30 days: not stored at all.
    assert not await jb.ingest(
        payload(clock, item_id="ancient", received_at=iso(clock() - PURGE_AFTER_S - 5))
    )
    got = await jb.rows()
    assert [r["older"] for r in got] == [False, True]
    async with WebClient(rig.h) as c:
        assert (await c.state())["just_bought"]["to_tag"] == 3  # older not counted
    clock.advance(PURGE_AFTER_S - OLDER_AFTER_S)
    assert await jb.housekeeping() == 1
    assert [r["older"] for r in await jb.rows()] == [True]
    assert rig.grocy.count_calls("POST", "/add") == 0  # nothing booked


# Grocy product matching -----------------------------------------------------------


async def test_tag_creates_grocy_product_from_lookup(
    rig: Rig,
    ha: FakeHomeAssistant,
    grocy: FakeGrocy,
    clock: FakeClock,
    observer: Observer,
) -> None:
    kroger = Kroger(ha)
    kroger.lookup["0001111042850"] = {
        "found": True,
        "name": "Kroger® Black Beans",
        "brand": "Kroger",
        "size": "15 oz",
    }
    kroger.recent = [payload(clock)]
    await start(rig, ha)
    await eventually(lambda: has_rows(rig, 1))
    row = (await rows(rig))[0]
    async with WebClient(rig.h) as c:
        reply = await c.post("/bought/tag", row=row["id"])
    assert reply == {
        "ok": True,
        "message": f"Tap each Kroger® Black Beans on the {STATION} station",
    }
    created = [p for p in grocy.products.values() if p.name == "Kroger® Black Beans"]
    assert len(created) == 1
    assert created[0].barcodes == ["0001111042850"]
    assert created[0].description == "Kroger · 15 oz"
    assert (await rows(rig))[0]["product_id"] == created[0].id
    # Start command for the station.
    await eventually(lambda: observer.last(START) is not None)
    assert observer.last(START) == {"barcode": "0001111042850", "count": 3}


async def test_existing_product_found_by_barcode(
    rig: Rig, ha: FakeHomeAssistant, grocy: FakeGrocy, clock: FakeClock
) -> None:
    beans = grocy.add_product("Beans", ["0001111042850"])
    Kroger(ha).recent = [payload(clock)]
    await start(rig, ha)
    await eventually(lambda: has_rows(rig, 1))
    jb = rig.h.just_bought
    assert jb is not None
    row = (
        await rig.store.get_received((await jb.rows())[0]["id"]) if rig.store else None
    )
    assert row is not None
    assert (await jb.product_for(row)).id == beans.id
    assert ha.rest("kroger_restock", "lookup_product") == []  # no lookup needed
    assert len(grocy.products) == 1


# Counted link session -------------------------------------------------------------


async def test_counted_session_stops_at_count_and_skips_duplicates(
    rig: Rig,
    ha: FakeHomeAssistant,
    grocy: FakeGrocy,
    clock: FakeClock,
    observer: Observer,
) -> None:
    beans = grocy.add_product("Beans", ["0001111042850"])
    Kroger(ha).recent = [payload(clock)]
    await start(rig, ha)
    assert rig.store is not None
    await rig.store.create_link(
        Link(EPCS[5], beans.id, "Beans", clock()), location_id=1
    )
    await eventually(lambda: has_rows(rig, 1))
    row_id = (await rows(rig))[0]["id"]
    # A station was used before: Tag them goes there.
    await observer.publish("pantry_hub/linking/kitchen/tag", {"epc": EPCS[4]})
    await eventually(lambda: rig.h.linking.last_station == "kitchen")
    rig.h.linking.last_station = STATION
    async with WebClient(rig.h) as c:
        assert (await c.post("/bought/tag", row=row_id))["ok"]
        await tap(observer, EPCS[0])
        await eventually(lambda: len(observer.json(RESULT)) == 1)
        assert observer.last(RESULT) == {
            "status": "linked",
            "product": "Beans",
            "message": "Linked 1 of 3: Beans",
            "barcode": "0001111042850",
            "epc": EPCS[0],
            "count": 3,
            "linked": 1,
            "remaining": 2,
        }
        session = (await c.state())["just_bought"]["session"]
        assert session["text"] == "1 of 3 linked"
        # Already linked to this product: skipped, not counted.
        await tap(observer, EPCS[5])
        await eventually(lambda: len(observer.json(RESULT)) == 2)
        skipped = observer.last(RESULT)
        assert skipped["status"] == "tag_already_linked"
        assert skipped["remaining"] == 2
        assert "skipped" in skipped["message"]
        await tap(observer, EPCS[1])
        await eventually(lambda: len(observer.json(RESULT)) == 3)
        await tap(observer, EPCS[2])
        await eventually(lambda: len(observer.json(RESULT)) == 4)
        assert observer.last(RESULT)["remaining"] == 0
        # Count reached: the session is over; a further tap isn't linked.
        await eventually(lambda: not rig.h.linking.active(STATION))
        await tap(observer, EPCS[3])
        await eventually(lambda: len(observer.json(RESULT)) == 5)
        assert observer.last(RESULT)["message"] == "Scan a barcode first"
        assert await rig.store.get_link(EPCS[3]) is None
        # The row is done: all 3 tagged.
        assert await rows(rig) == []
        state = await c.state()
        assert state["just_bought"]["session"] is None
    # Tagging never touches Grocy stock by itself.
    assert grocy.count_calls("POST", "/add") == 0


async def test_counted_session_times_out(
    rig: Rig,
    ha: FakeHomeAssistant,
    grocy: FakeGrocy,
    clock: FakeClock,
    observer: Observer,
) -> None:
    grocy.add_product("Beans", ["0001111042850"])
    Kroger(ha).recent = [payload(clock)]
    await start(rig, ha, counted_idle=0.4)
    await eventually(lambda: has_rows(rig, 1))
    row_id = (await rows(rig))[0]["id"]
    assert rig.h.just_bought is not None
    await rig.h.tag_received(row_id)
    await tap(observer, EPCS[0])
    await eventually(lambda: len(observer.json(RESULT)) == 1)
    await eventually(lambda: len(observer.json(RESULT)) == 2, timeout=3)
    assert observer.last(RESULT)["status"] == "timeout"
    assert observer.last(RESULT)["message"] == "No tag for 0.4 s: 1 of 3 linked"
    left = await rows(rig)
    assert left[0]["tagged"] == 1 and left[0]["to_tag"] == 2
    # Tag them again: count is what's left.
    await rig.h.tag_received(row_id)
    await eventually(lambda: len(observer.json(START)) == 2)
    assert observer.last(START)["count"] == 2


async def test_barcode_scan_replaces_counted_session(
    rig: Rig,
    ha: FakeHomeAssistant,
    grocy: FakeGrocy,
    clock: FakeClock,
    observer: Observer,
) -> None:
    grocy.add_product("Beans", ["0001111042850"])
    grocy.add_product("Rice", ["0002"])
    Kroger(ha).recent = [payload(clock)]
    await start(rig, ha)
    await eventually(lambda: has_rows(rig, 1))
    await rig.h.tag_received((await rows(rig))[0]["id"])
    await observer.publish(f"pantry_hub/linking/{STATION}/barcode", {"barcode": "0002"})
    await asyncio.sleep(0.05)
    await tap(observer, EPCS[0])
    await eventually(lambda: observer.last(RESULT) is not None)
    assert observer.last(RESULT)["product"] == "Rice"
    assert "count" not in observer.last(RESULT)


# Not tagging + no double counting ------------------------------------------------


async def test_not_tagging_books_purchase_and_later_tag_does_not_double_count(
    rig: Rig,
    ha: FakeHomeAssistant,
    grocy: FakeGrocy,
    clock: FakeClock,
    observer: Observer,
) -> None:
    beans = grocy.add_product("Beans", ["0001111042850"])
    Kroger(ha).recent = [payload(clock)]
    await start(rig, ha)
    await eventually(lambda: has_rows(rig, 1))
    row_id = (await rows(rig))[0]["id"]
    async with WebClient(rig.h) as c:
        reply = await c.post("/bought/skip", row=row_id, add_stock="on")
        assert reply["message"] == "Added 3 × Beans to Grocy stock"
        again = await c.post("/bought/skip", row=row_id, add_stock="on")
        assert again["ok"] is False  # a double click books once
    adds = [b for m, p, b in grocy.calls if m == "POST" and p.endswith("/add")]
    assert adds == [
        {
            "amount": 3,
            "transaction_type": "purchase",
            "location_id": 1,
            "note": "Pantry Hub: Kroger delivery ORD-1",
            "price": 0.89,
        }
    ]
    assert grocy.amount(beans.id) == 3
    assert await rows(rig) == []
    assert rig.store is not None
    assert await rig.store.booked_credits() == {beans.id: 3}
    # Two days later one of those cans gets a tag at the station …
    clock.advance(2 * 86400)
    await observer.publish(
        "pantry_hub/linking/kitchen/barcode", {"barcode": "0001111042850"}
    )
    await asyncio.sleep(0.05)
    await observer.publish("pantry_hub/linking/kitchen/tag", {"epc": EPCS[0]})
    res = "pantry_hub/linking/kitchen/result"
    await eventually(lambda: observer.last(res) is not None)
    assert "already in stock" in observer.last(res)["message"]
    # … and goes into the pantry: matched, not added again.
    await rig.arrive(EPCS[0])
    await rig.idle()
    assert grocy.amount(beans.id) == 3
    assert await rig.store.booked_credits() == {beans.id: 2}
    assert rig.h.recent_events[0]["kind"] == "matched_booked"
    # A second can, tagged from the web UI: matched too.
    await rig.h.relink(EPCS[1], barcode="0001111042850")
    await rig.arrive(EPCS[1])
    await rig.idle()
    assert grocy.amount(beans.id) == 3
    assert await rig.store.booked_credits() == {beans.id: 1}
    # After 7 days the last booked unit no longer matches: a new tag is a
    # normal new unit whose arrival adds stock.
    clock.advance(BOOKED_MATCH_S)
    link = await rig.h.relink(EPCS[2], barcode="0001111042850")
    stored = await rig.store.get_link(link.epc)
    assert stored is not None and stored.skip_add_cycle is None
    assert await rig.store.booked_credits() == {}


async def test_not_tagging_without_stock_and_partial(
    rig: Rig,
    ha: FakeHomeAssistant,
    grocy: FakeGrocy,
    clock: FakeClock,
    observer: Observer,
) -> None:
    beans = grocy.add_product("Beans", ["0001111042850"])
    Kroger(ha).recent = [
        payload(clock),
        payload(clock, item_id="it-2", upc="0002", name="Oats", quantity_received=2),
    ]
    await start(rig, ha)
    await eventually(lambda: has_rows(rig, 2))
    by_name = {r["name"]: r["id"] for r in await rows(rig)}
    async with WebClient(rig.h) as c:
        # Unticked: just removed.
        reply = await c.post("/bought/skip", row=by_name["Oats"], add_stock="")
        assert reply["message"] == "Removed Oats"
        assert grocy.count_calls("POST", "/add") == 0
        assert not [p for p in grocy.products.values() if p.name == "Oats"]
        # One of three tagged, then Not tagging books the other two.
        await c.post("/bought/tag", row=by_name["Kroger Black Beans"])
        await tap(observer, EPCS[0])
        await eventually(lambda: len(observer.json(RESULT)) == 1)
        reply = await c.post("/bought/skip", row=by_name["Kroger Black Beans"])
        assert reply["message"] == "Added 2 × Beans to Grocy stock"
    assert grocy.amount(beans.id) == 2
    assert rig.store is not None
    assert await rig.store.booked_credits() == {beans.id: 2}


async def test_purchase_refused_keeps_the_row(
    rig: Rig, ha: FakeHomeAssistant, grocy: FakeGrocy, clock: FakeClock
) -> None:
    grocy.add_product("Beans", ["0001111042850"])
    Kroger(ha).recent = [payload(clock)]
    await start(rig, ha)
    await eventually(lambda: has_rows(rig, 1))
    row_id = (await rows(rig))[0]["id"]
    grocy.fault("/api/stock/products/{id}/add", "400")
    async with WebClient(rig.h) as c:
        reply = await c.post("/bought/skip", row=row_id)
    assert reply["ok"] is False and "refused" in reply["message"]
    assert [r["id"] for r in await rows(rig)] == [row_id]
    assert rig.store is not None
    assert await rig.store.booked_credits() == {}
