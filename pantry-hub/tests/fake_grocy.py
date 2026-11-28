"""A small stateful fake of the Grocy REST API (the endpoints the hub uses).

Runs as a real aiohttp server on 127.0.0.1 so the real :class:`GrocyClient`
is exercised end to end. ``faults`` injects failures per route.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
import itertools
from typing import Any

from aiohttp import web

API_KEY = "test-key"
BODY = web.RequestKey("body", object)


@dataclass
class FakeProduct:
    id: int
    name: str
    barcodes: list[str] = field(default_factory=list)
    default_best_before_days: int = 0
    #: PNG bytes served as the product picture (None: no picture).
    picture: bytes | None = None
    description: str = ""
    #: picture_file_name; defaults to product-<id>.png when ``picture`` is set.
    picture_file_name: str | None = None


@dataclass
class Entry:
    stock_id: str
    product_id: int
    amount: float
    location_id: int
    note: str
    best_before_date: str | None


class FakeGrocy:
    """In-memory Grocy. ``faults[route]`` is a deque of fault names.

    Fault names: ``"500"``, ``"400"``, ``"timeout"`` (hang before applying),
    ``"apply_then_500"`` (apply, then answer 500), ``"apply_then_hang"``
    (apply, then never answer in time).
    """

    def __init__(self) -> None:
        self.products: dict[int, FakeProduct] = {}
        self.locations: dict[int, str] = {1: "Pantry", 2: "Fridge"}
        self.entries: list[Entry] = []
        self.lookup: dict[str, str] = {}  # barcode → name the plugin finds
        self.faults: dict[str, deque[str]] = {}
        self.calls: list[tuple[str, str, Any]] = []
        self.hang_s = 30.0
        self._ids = itertools.count(1)
        self._product_ids = itertools.count(100)
        self._runner: web.AppRunner | None = None
        self.url = ""
        self.down = False
        #: Uploaded product picture files by name.
        self.files: dict[str, bytes] = {}
        self.quantity_units = [{"id": 2, "name": "Piece"}]

    # Setup -----------------------------------------------------------------

    def add_product(
        self, name: str, barcodes: list[str] | None = None, days: int = 0
    ) -> FakeProduct:
        pid = next(self._product_ids)
        product = FakeProduct(pid, name, list(barcodes or []), days)
        self.products[pid] = product
        return product

    def fault(self, route: str, *names: str) -> None:
        self.faults.setdefault(route, deque()).extend(names)

    def amount(self, product_id: int, location_id: int = 1) -> float:
        return sum(
            e.amount
            for e in self.entries
            if e.product_id == product_id and e.location_id == location_id
        )

    def count_calls(self, method: str, suffix: str) -> int:
        return sum(1 for m, p, _ in self.calls if m == method and p.endswith(suffix))

    async def start(self) -> None:
        app = web.Application(middlewares=[self._mw])
        r = app.router
        r.add_get("/api/system/info", self.system_info)
        r.add_get("/api/objects/locations/{id}", self.location)
        r.add_get("/api/stock/products/by-barcode/{barcode}", self.by_barcode)
        r.add_get("/api/stock/barcodes/external-lookup/{barcode}", self.external)
        r.add_get("/api/stock/products/{id}", self.product)
        r.add_get("/api/stock/products/{id}/entries", self.product_entries)
        r.add_get("/api/files/productpictures/{name}", self.picture)
        r.add_put("/api/files/productpictures/{name}", self.put_picture)
        r.add_get("/api/objects/products/{id}", self.product_object)
        r.add_put("/api/objects/products/{id}", self.update_product)
        r.add_post("/api/objects/products", self.create_product)
        r.add_post("/api/objects/product_barcodes", self.create_barcode)
        r.add_get("/api/objects/product_barcodes", self.list_barcodes)
        r.add_get("/api/objects/quantity_units", self.units)
        r.add_post("/api/stock/products/{id}/add", self.add)
        r.add_post("/api/stock/products/{id}/consume", self.consume)
        r.add_post("/api/stock/products/{id}/inventory", self.inventory)
        r.add_get("/api/stock/locations/{id}/entries", self.location_entries)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        self.url = f"http://127.0.0.1:{port}"

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    # Plumbing --------------------------------------------------------------

    def _route(self, request: web.Request) -> str:
        info = request.match_info.route.resource
        return info.canonical if info is not None else request.path

    @web.middleware
    async def _mw(self, request: web.Request, handler: Any) -> web.StreamResponse:
        if not request.can_read_body:
            body = None
        elif request.content_type == "application/json":
            body = await request.json()
        else:
            body = await request.read()
        self.calls.append((request.method, request.path, body))
        if self.down:
            raise web.HTTPServiceUnavailable(text="down")
        if request.headers.get("GROCY-API-KEY") != API_KEY:
            return web.json_response({"error_message": "Unauthorized"}, status=401)
        request[BODY] = body
        route = self._route(request)
        queue = self.faults.get(route)
        fault = queue.popleft() if queue else None
        if fault == "500":
            return web.json_response({"error_message": "boom"}, status=500)
        if fault == "400":
            return web.json_response({"error_message": "bad request"}, status=400)
        if fault == "timeout":
            await asyncio.sleep(self.hang_s)
        resp: web.StreamResponse = await handler(request)
        if fault == "apply_then_500":
            return web.json_response({"error_message": "late boom"}, status=500)
        if fault == "apply_then_hang":
            await asyncio.sleep(self.hang_s)
        return resp

    def _pid(self, request: web.Request) -> FakeProduct:
        pid = int(request.match_info["id"])
        if pid not in self.products:
            raise web.HTTPBadRequest(
                text='{"error_message": "Product does not exist"}',
                content_type="application/json",
            )
        return self.products[pid]

    @staticmethod
    def _pjson(p: FakeProduct) -> dict[str, Any]:
        return {
            "id": p.id,
            "name": p.name,
            "default_best_before_days": p.default_best_before_days,
            "picture_file_name": FakeGrocy.picture_name(p),
            "description": p.description,
        }

    # Handlers --------------------------------------------------------------

    async def system_info(self, request: web.Request) -> web.Response:
        return web.json_response({"grocy_version": {"Version": "4.7.1"}})

    async def location(self, request: web.Request) -> web.Response:
        lid = int(request.match_info["id"])
        if lid not in self.locations:
            return web.json_response({"error_message": "not found"}, status=404)
        return web.json_response({"id": lid, "name": self.locations[lid]})

    async def by_barcode(self, request: web.Request) -> web.Response:
        code = request.match_info["barcode"]
        for p in self.products.values():
            if code in p.barcodes:
                return web.json_response({"product": self._pjson(p)})
        return web.json_response(
            {"error_message": f"No product with barcode {code} found"}, status=400
        )

    async def external(self, request: web.Request) -> web.Response:
        code = request.match_info["barcode"]
        name = self.lookup.get(code)
        if name is None:
            return web.json_response(None)
        out: dict[str, Any] = {"name": name, "barcode": code}
        if request.query.get("add") == "true":
            out["id"] = self.add_product(name, [code]).id
        return web.json_response(out)

    async def product(self, request: web.Request) -> web.Response:
        p = self._pid(request)
        return web.json_response(
            {"product": self._pjson(p), "stock_amount": self.amount(p.id)}
        )

    @staticmethod
    def picture_name(p: FakeProduct) -> str | None:
        if p.picture_file_name is not None:
            return p.picture_file_name
        return f"product-{p.id}.png" if p.picture else None

    async def picture(self, request: web.Request) -> web.Response:
        import base64

        name = base64.b64decode(request.match_info["name"]).decode()
        assert request.query.get("force_serve_as") == "picture"
        if name in self.files:
            return web.Response(body=self.files[name], content_type="image/jpeg")
        for p in self.products.values():
            if p.picture is not None and name == self.picture_name(p):
                return web.Response(body=p.picture, content_type="image/png")
        return web.Response(status=404)

    async def put_picture(self, request: web.Request) -> web.Response:
        import base64

        name = base64.b64decode(request.match_info["name"]).decode()
        body = request[BODY]
        assert isinstance(body, bytes) and body
        self.files[name] = body
        return web.Response(status=204)

    async def product_object(self, request: web.Request) -> web.Response:
        return web.json_response(self._pjson(self._pid(request)))

    async def update_product(self, request: web.Request) -> web.Response:
        p = self._pid(request)
        body: Any = request[BODY]
        for key, value in body.items():
            if key == "name":
                p.name = value
            elif key == "description":
                p.description = value
            elif key == "picture_file_name":
                p.picture_file_name = value
        return web.Response(status=204)

    async def create_product(self, request: web.Request) -> web.Response:
        body: Any = request[BODY]
        assert body["qu_id_stock"] == self.quantity_units[0]["id"]
        p = self.add_product(str(body["name"]))
        p.description = str(body.get("description", ""))
        return web.json_response({"created_object_id": p.id})

    async def create_barcode(self, request: web.Request) -> web.Response:
        body: Any = request[BODY]
        self.products[int(body["product_id"])].barcodes.append(str(body["barcode"]))
        return web.json_response({"created_object_id": 1})

    async def list_barcodes(self, request: web.Request) -> web.Response:
        query = request.query.get("query[]", "")
        assert query.startswith("product_id=")
        p = self.products.get(int(query.split("=", 1)[1]))
        rows = [] if p is None else p.barcodes
        return web.json_response(
            [{"product_id": p.id, "barcode": b} for b in rows] if p else []
        )

    async def units(self, request: web.Request) -> web.Response:
        return web.json_response(self.quantity_units)

    async def product_entries(self, request: web.Request) -> web.Response:
        p = self._pid(request)
        return web.json_response(
            [
                {
                    "id": i,
                    "stock_id": e.stock_id,
                    "product_id": e.product_id,
                    "amount": e.amount,
                    "location_id": e.location_id,
                    "note": e.note,
                    "best_before_date": e.best_before_date,
                }
                for i, e in enumerate(self.entries)
                if e.product_id == p.id
            ]
        )

    async def add(self, request: web.Request) -> web.Response:
        p = self._pid(request)
        body: Any = request[BODY]
        stock_id = f"s{next(self._ids):05d}"
        self.entries.append(
            Entry(
                stock_id=stock_id,
                product_id=p.id,
                amount=float(body["amount"]),
                location_id=int(body.get("location_id", 1)),
                note=str(body.get("note", "")),
                best_before_date=body.get("best_before_date"),
            )
        )
        return web.json_response(
            [
                {
                    "id": 1,
                    "product_id": p.id,
                    "amount": body["amount"],
                    "stock_id": stock_id,
                    "transaction_type": "purchase",
                }
            ]
        )

    def total(self, product_id: int) -> float:
        return sum(e.amount for e in self.entries if e.product_id == product_id)

    async def inventory(self, request: web.Request) -> web.Response:
        """Set the total amount; like Grocy, an inventory correction."""
        p = self._pid(request)
        body: Any = request[BODY]
        new = float(body["new_amount"])
        loc = int(body.get("location_id", 1))
        diff = new - self.total(p.id)
        if diff > 0:
            self.entries.append(
                Entry(
                    stock_id=f"s{next(self._ids):05d}",
                    product_id=p.id,
                    amount=diff,
                    location_id=loc,
                    note=str(body.get("note", "")),
                    best_before_date=None,
                )
            )
        while diff < 0:
            # Grocy consumes by its default rule; the fake takes the given
            # location first, then any other.
            mine = [e for e in self.entries if e.product_id == p.id]
            mine.sort(key=lambda e: e.location_id != loc)
            e = mine[0]
            take = min(e.amount, -diff)
            e.amount -= take
            diff += take
            if e.amount <= 0:
                self.entries.remove(e)
        return web.json_response(
            [{"id": 9, "transaction_type": "inventory-correction"}]
        )

    async def location_entries(self, request: web.Request) -> web.Response:
        loc = int(request.match_info["id"])
        return web.json_response(
            [
                {
                    "id": i,
                    "stock_id": e.stock_id,
                    "product_id": e.product_id,
                    "amount": e.amount,
                    "location_id": e.location_id,
                    "note": e.note,
                }
                for i, e in enumerate(self.entries)
                if e.location_id == loc
            ]
        )

    async def consume(self, request: web.Request) -> web.Response:
        p = self._pid(request)
        body: Any = request[BODY]
        sid = body.get("stock_entry_id")
        for i, e in enumerate(self.entries):
            if e.product_id != p.id:
                continue
            if sid and e.stock_id != sid:
                continue
            if (
                not sid
                and "location_id" in body
                and e.location_id != body["location_id"]
            ):
                continue
            del self.entries[i]
            return web.json_response([{"id": 2, "stock_id": e.stock_id}])
        return web.json_response(
            {"error_message": "Amount to be consumed cannot be > current stock amount"},
            status=400,
        )
