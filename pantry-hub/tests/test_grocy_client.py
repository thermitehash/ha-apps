from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Any

import aiohttp
from aiohttp import web
import pytest

from pantry_hub.grocy import (
    GrocyAuthError,
    GrocyClient,
    GrocyError,
    GrocyRequestError,
    GrocyUnavailable,
)

from .fake_grocy import API_KEY, FakeGrocy

Reply = tuple[int, Any] | Callable[[], Any]


class Scripted:
    """Tiny HTTP server answering each path with a scripted reply."""

    def __init__(self) -> None:
        self.replies: dict[str, Reply] = {}
        self.url = ""
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self.handle)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        self.url = f"http://127.0.0.1:{port}"

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    async def handle(self, request: web.Request) -> web.StreamResponse:
        reply = self.replies[request.path]
        if callable(reply):
            result = reply()
            if asyncio.iscoroutine(result):
                result = await result
            if isinstance(result, web.StreamResponse):
                return result
            reply = result
        status, body = reply
        if isinstance(body, str):
            return web.Response(status=status, text=body)
        return web.json_response(body, status=status)


@pytest.fixture
async def server() -> AsyncIterator[Scripted]:
    s = Scripted()
    await s.start()
    yield s
    await s.stop()


@pytest.fixture
async def client(server: Scripted) -> AsyncIterator[GrocyClient]:
    c = GrocyClient(server.url, "k", timeout=0.5)
    yield c
    await c.close()


async def test_status_mapping(server: Scripted, client: GrocyClient) -> None:
    server.replies["/api/system/info"] = (401, {"error_message": "no"})
    with pytest.raises(GrocyAuthError):
        await client.system_info()
    server.replies["/api/system/info"] = (503, "busy")
    with pytest.raises(GrocyUnavailable) as err:
        await client.system_info()
    assert err.value.maybe_applied is False  # GET
    server.replies["/api/stock/products/1/add"] = (500, {"error_message": "x"})
    with pytest.raises(GrocyUnavailable) as err2:
        await client.add(1, location_id=1, best_before=None, note="n")
    assert err2.value.maybe_applied is True
    server.replies["/api/stock/products/1/consume"] = (400, {"error_message": "nope"})
    with pytest.raises(GrocyRequestError) as err3:
        await client.consume(1, stock_entry_id="s", location_id=1)
    assert err3.value.status == 400 and "nope" in str(err3.value)


async def test_timeouts_and_disconnects(server: Scripted, client: GrocyClient) -> None:
    async def hang() -> tuple[int, Any]:
        await asyncio.sleep(1)
        return (200, [])

    server.replies["/api/stock/products/1/add"] = hang
    with pytest.raises(GrocyUnavailable) as err:
        await client.add(1, location_id=1, best_before=None, note="n")
    assert err.value.maybe_applied is True
    assert client.reachable is False

    async def drop() -> web.StreamResponse:
        raise ConnectionResetError

    server.replies["/api/stock/products/2/add"] = drop
    with pytest.raises(GrocyUnavailable):
        await client.add(2, location_id=1, best_before=None, note="n")


async def test_odd_payloads(server: Scripted, client: GrocyClient) -> None:
    r = server.replies
    r["/api/system/info"] = (200, "<html>not grocy</html>")
    with pytest.raises(GrocyError):
        await client.system_info()
    r["/api/stock/products/1/entries"] = (200, {"x": 1})
    with pytest.raises(GrocyError):
        await client.entries(1)
    r["/api/stock/products/1/add"] = (200, [])
    assert await client.add(1, location_id=1, best_before=None, note="n") is None
    r["/api/objects/locations/3"] = (200, [])
    with pytest.raises(GrocyRequestError):
        await client.location_name(3)
    r["/api/stock/products/1"] = (200, [])
    with pytest.raises(GrocyError):
        await client.product(1)
    r["/api/stock/products/by-barcode/1"] = (200, [])
    with pytest.raises(GrocyError):
        await client.product_by_barcode("1")
    r["/api/stock/products/by-barcode/2"] = (500, "x")
    with pytest.raises(GrocyUnavailable):
        await client.product_by_barcode("2")
    r["/api/stock/products/by-barcode/3"] = (405, "x")
    with pytest.raises(GrocyRequestError):
        await client.product_by_barcode("3")
    r["/api/stock/barcodes/external-lookup/4"] = (200, {"name": "no id"})
    assert await client.external_lookup("4") is None
    r["/api/stock/products/5/entries"] = (200, ["junk", {"stock_id": "a", "amount": 2}])
    entries = await client.entries(5)
    assert (
        len(entries) == 1 and entries[0].amount == 2 and entries[0].location_id is None
    )


async def test_connection_refused() -> None:
    c = GrocyClient("http://127.0.0.1:9", "k", timeout=2)
    try:
        with pytest.raises(GrocyUnavailable) as err:
            await c.system_info()
        assert err.value.maybe_applied is False
    finally:
        await c.close()


async def test_against_fake_grocy(grocy: FakeGrocy) -> None:
    p = grocy.add_product("Beans", ["0001"], days=7)
    grocy.lookup["5555"] = "Oat milk"
    c = GrocyClient(grocy.url, API_KEY)
    try:
        info = await c.system_info()
        assert info["grocy_version"]["Version"] == "4.7.1"
        assert await c.location_name(1) == "Pantry"
        prod = await c.product_by_barcode("0001")
        assert (
            prod is not None and prod.id == p.id and prod.default_best_before_days == 7
        )
        assert await c.product_by_barcode("nope") is None
        assert await c.external_lookup("nothing") is None
        created = await c.external_lookup("5555")
        assert created is not None and created.name == "Oat milk"
        sid = await c.add(p.id, location_id=1, best_before="2030-01-01", note="hello")
        assert sid is not None
        entries = await c.entries(p.id)
        assert [(e.stock_id, e.note, e.location_id) for e in entries] == [
            (sid, "hello", 1)
        ]
        await c.consume(p.id, stock_entry_id=sid, location_id=1)
        assert await c.entries(p.id) == []
        with pytest.raises(GrocyRequestError):
            await c.consume(p.id, stock_entry_id=None, location_id=1)
        grocy.fault("/api/stock/barcodes/external-lookup/{barcode}", "400")
        assert await c.external_lookup("5555") is None
    finally:
        await c.close()


async def test_shared_session_not_closed(grocy: FakeGrocy) -> None:
    async with aiohttp.ClientSession() as session:
        c = GrocyClient(grocy.url, API_KEY, session=session)
        await c.system_info()
        await c.close()
        assert not session.closed
