from __future__ import annotations

import asyncio
import json

import pytest

from pantry_hub.grocy import Product
from pantry_hub.linking import (
    LinkingManager,
    LinkResult,
    best_before_for,
    normalize_epc,
)
from pantry_hub.store import Link, Store

from .conftest import eventually
from .memory_backend import MemoryBackend

EPC1 = "E2000017221101441890ABCD"
EPC2 = "E2000017221101441890ABCE"


class Results:
    def __init__(self) -> None:
        self.items: list[tuple[str, LinkResult]] = []

    async def __call__(self, station: str, result: LinkResult) -> None:
        self.items.append((station, result))

    @property
    def statuses(self) -> list[str]:
        return [r.status for _, r in self.items]


def bc(code: str) -> bytes:
    return json.dumps({"barcode": code, "ts": 1}).encode()


def tag(epc: str, rssi: float | None = -50.0) -> bytes:
    return json.dumps({"epc": epc, "rssi": rssi, "ts": 1}).encode()


@pytest.fixture
def backend() -> MemoryBackend:
    b = MemoryBackend()
    b.add_product(1, "Black beans", barcode="0001", days=30)
    return b


def manager(
    store: Store, backend: MemoryBackend, results: Results, **kw: float
) -> LinkingManager:
    linked: list[tuple[Link, int | None]] = []

    async def on_linked(link: Link, op_id: int | None) -> None:
        linked.append((link, op_id))

    m = LinkingManager(
        store,
        backend,
        publish=results,
        location_id=1,
        on_linked=on_linked,
        tag_timeout=kw.get("tag_timeout", 0.5),
        rssi_window=kw.get("rssi_window", 0.05),
    )
    m.linked = linked  # type: ignore[attr-defined]
    return m


async def test_link_picks_strongest_rssi(store: Store, backend: MemoryBackend) -> None:
    results = Results()
    m = manager(store, backend, results, rssi_window=0.1)
    await m.handle_barcode("st1", bc("0001"))
    await m.handle_tag("st1", tag(EPC1, -60))
    await m.handle_tag("st1", tag(EPC2, -40))
    await m.handle_tag("st1", tag(EPC1, -55))
    await eventually(lambda: results.items != [])
    assert results.statuses == ["linked"]
    _, r = results.items[0]
    assert r.epc == EPC2 and r.product == "Black beans"
    link = await store.get_link(EPC2)
    assert (
        link is not None
        and link.product_id == 1
        and link.default_best_before_days == 30
    )
    assert m.last_link == link
    assert not m.active("st1")


async def test_late_tag_after_window_is_ignored(
    store: Store, backend: MemoryBackend
) -> None:
    results = Results()
    m = manager(store, backend, results, rssi_window=0.1)
    await m.handle_barcode("st1", bc("0001"))
    await m.handle_tag("st1", tag(EPC1, -70))
    await asyncio.sleep(0.15)
    await m.handle_tag("st1", tag(EPC2, -10))  # neighbour, after the first second
    await eventually(lambda: results.items != [])
    assert results.items[0][1].epc == EPC1


def test_choose_handles_unknown_rssi() -> None:
    assert LinkingManager.choose({"A": None, "B": -80.0}) == "B"
    assert LinkingManager.choose({"A": None, "B": None}) == "A"
    assert LinkingManager.choose({"A": -50.0, "B": -50.0}) == "A"


async def test_timeout_when_no_tag(store: Store, backend: MemoryBackend) -> None:
    results = Results()
    m = manager(store, backend, results, tag_timeout=0.1)
    await m.handle_barcode("st1", bc("0001"))
    await eventually(lambda: results.items != [])
    assert results.statuses == ["timeout"]
    assert results.items[0][1].product == "Black beans"


async def test_unknown_barcode_goes_to_needs_product(
    store: Store, backend: MemoryBackend
) -> None:
    results = Results()
    m = manager(store, backend, results)
    await m.handle_barcode("st1", bc("9999"))
    await eventually(lambda: results.items != [])
    assert results.statuses == ["unknown_barcode"]
    assert [n.barcode for n in await store.needs_product()] == ["9999"]
    await m.handle_barcode("st1", bc("9999"))
    await eventually(lambda: len(results.items) == 2)
    assert (await store.needs_product())[0].scans == 2


async def test_external_lookup_creates_product(
    store: Store, backend: MemoryBackend
) -> None:
    backend.lookup["7777"] = Product(7, "Oat milk")
    results = Results()
    m = manager(store, backend, results)
    await m.handle_barcode("st1", bc("7777"))
    await m.handle_tag("st1", tag(EPC1))
    await eventually(lambda: results.items != [])
    assert results.statuses == ["linked"]
    assert results.items[0][1].product == "Oat milk"


async def test_tag_already_linked(store: Store, backend: MemoryBackend) -> None:
    await store.create_link(Link(EPC1, 5, "Rice", 1.0), location_id=1)
    results = Results()
    m = manager(store, backend, results)
    await m.handle_barcode("st1", bc("0001"))
    await m.handle_tag("st1", tag(EPC1))
    await eventually(lambda: results.items != [])
    assert results.statuses == ["tag_already_linked"]
    assert results.items[0][1].product == "Rice"
    link = await store.get_link(EPC1)
    assert link is not None and link.product_id == 5  # unchanged


async def test_tag_without_barcode_is_an_error(
    store: Store, backend: MemoryBackend
) -> None:
    results = Results()
    m = manager(store, backend, results)
    await m.handle_tag("st1", tag(EPC1))
    assert results.statuses == ["error"]


@pytest.mark.parametrize(
    "payload",
    [
        b"not json",
        b"[]",
        json.dumps({"barcode": ""}).encode(),
        json.dumps({"barcode": "x" * 65}).encode(),
        b"\xff",
    ],
)
async def test_bad_barcode_payload(
    store: Store, backend: MemoryBackend, payload: bytes
) -> None:
    results = Results()
    m = manager(store, backend, results)
    await m.handle_barcode("st1", payload)
    assert results.statuses == ["error"]


async def test_numeric_barcode_is_accepted(
    store: Store, backend: MemoryBackend
) -> None:
    results = Results()
    m = manager(store, backend, results, tag_timeout=0.05)
    await m.handle_barcode("st1", json.dumps({"barcode": 1}).encode())
    await eventually(lambda: results.items != [])
    assert results.statuses == ["unknown_barcode"]


@pytest.mark.parametrize(
    "payload",
    [b"{}", json.dumps({"epc": "XYZ"}).encode(), json.dumps({"epc": "ABC"}).encode()],
)
async def test_bad_tag_payload(
    store: Store, backend: MemoryBackend, payload: bytes
) -> None:
    results = Results()
    m = manager(store, backend, results)
    await m.handle_tag("st1", payload)
    assert results.statuses == ["error"]


async def test_invalid_station_ids_are_ignored(
    store: Store, backend: MemoryBackend
) -> None:
    results = Results()
    m = manager(store, backend, results)
    await m.handle_barcode("bad id!", bc("0001"))
    await m.handle_tag("x" * 40, tag(EPC1))
    assert results.items == []


async def test_new_barcode_replaces_session(
    store: Store, backend: MemoryBackend
) -> None:
    backend.add_product(2, "Rice", barcode="0002")
    results = Results()
    m = manager(store, backend, results)
    await m.handle_barcode("st1", bc("0001"))
    await m.handle_barcode("st1", bc("0002"))
    await m.handle_tag("st1", tag(EPC1))
    await eventually(lambda: results.items != [])
    assert results.statuses == ["linked"]
    assert results.items[0][1].product == "Rice"


async def test_grocy_down_reports_error(store: Store, backend: MemoryBackend) -> None:
    backend.faults.append("down")
    results = Results()
    m = manager(store, backend, results)
    await m.handle_barcode("st1", bc("0001"))
    await eventually(lambda: results.items != [])
    assert results.statuses == ["error"]
    assert "unavailable" in results.items[0][1].message


async def test_grocy_auth_error_reports_error(
    store: Store, backend: MemoryBackend
) -> None:
    backend.faults.append("auth")
    results = Results()
    m = manager(store, backend, results)
    await m.handle_barcode("st1", bc("0001"))
    await eventually(lambda: results.items != [])
    assert results.statuses == ["error"]


async def test_close_cancels_sessions(store: Store, backend: MemoryBackend) -> None:
    results = Results()
    m = manager(store, backend, results, tag_timeout=10)
    await m.handle_barcode("st1", bc("0001"))
    await asyncio.sleep(0.01)
    await m.close()
    assert results.items == []


async def test_stations_are_independent(store: Store, backend: MemoryBackend) -> None:
    results = Results()
    m = manager(store, backend, results)
    await m.handle_barcode("st1", bc("0001"))
    await m.handle_barcode("st2", bc("0001"))
    await m.handle_tag("st1", tag(EPC1))
    await m.handle_tag("st2", tag(EPC2))
    await eventually(lambda: len(results.items) == 2)
    assert sorted(s for s, _ in results.items) == ["st1", "st2"]


def test_normalize_epc() -> None:
    assert normalize_epc("e200 0017") == "E2000017"
    assert normalize_epc("E2:00:00:17") == "E2000017"
    assert normalize_epc("E20") is None
    assert normalize_epc(5) is None


def test_best_before() -> None:
    now = 1_800_000_000.0  # 2027-01-15
    base = Link("A", 1, "x", now)
    assert best_before_for(base, now) is None
    assert (
        best_before_for(Link("A", 1, "x", now, best_before="2027-02-01"), now)
        == "2027-02-01"
    )
    assert (
        best_before_for(Link("A", 1, "x", now, default_best_before_days=-1), now)
        == "2999-12-31"
    )
    assert (
        best_before_for(Link("A", 1, "x", now, default_best_before_days=10), now)
        == "2027-01-25"
    )


def test_result_payload() -> None:
    p = LinkResult("linked", "ok", product="Beans", barcode="1", epc="AB").payload()
    assert p == {
        "status": "linked",
        "product": "Beans",
        "message": "ok",
        "barcode": "1",
        "epc": "AB",
    }
    assert set(LinkResult("timeout", "t").payload()) == {"status", "product", "message"}
