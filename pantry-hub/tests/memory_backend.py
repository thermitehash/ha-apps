"""In-memory StockBackend with scripted faults (unit tests of worker/linking)."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import itertools

from pantry_hub.grocy import (
    GrocyAuthError,
    GrocyRequestError,
    GrocyUnavailable,
    Product,
    StockEntry,
)


@dataclass
class MemoryBackend:
    products: dict[int, Product] = field(default_factory=dict)
    barcodes: dict[str, int] = field(default_factory=dict)
    lookup: dict[str, Product] = field(default_factory=dict)
    stock: list[StockEntry] = field(default_factory=list)
    # Each fault: "down" (not sent), "lost" (applied, answer lost), "reject",
    # "auth", "late" (add times out unapplied, then lands two calls later)
    faults: deque[str] = field(default_factory=deque)
    log: list[tuple[str, int, str | None]] = field(default_factory=list)
    notes: dict[str, str] = field(default_factory=dict)
    _ids: itertools.count[int] = field(default_factory=lambda: itertools.count(1))
    _late: list[list[object]] = field(default_factory=list)
    calls: int = 0

    def add_product(
        self, pid: int, name: str, barcode: str | None = None, days: int | None = None
    ) -> Product:
        p = Product(pid, name, days)
        self.products[pid] = p
        if barcode:
            self.barcodes[barcode] = pid
        return p

    def _fault(self) -> str | None:
        self.calls += 1
        # Deliver late adds whose countdown ran out (Grocy finishing a request
        # the client already gave up on).
        for item in list(self._late):
            item[0] = int(item[0]) - 1  # type: ignore[call-overload]
            if int(item[0]) <= 0:  # type: ignore[call-overload]
                self._late.remove(item)
                self._apply_add(int(item[1]), int(item[2]), str(item[3]))  # type: ignore[call-overload]
        return self.faults.popleft() if self.faults else None

    def _apply_add(self, product_id: int, location_id: int, note: str) -> str:
        sid = f"s{next(self._ids)}"
        self.stock.append(StockEntry(sid, product_id, 1.0, location_id, note))
        self.log.append(("add", product_id, sid))
        self.notes[sid] = note
        return sid

    def _pre(self, fault: str | None) -> None:
        if fault == "down":
            raise GrocyUnavailable("connection refused", maybe_applied=False)
        if fault == "reject":
            raise GrocyRequestError(400, "rejected")
        if fault == "auth":
            raise GrocyAuthError("Grocy refused the API key (HTTP 401)")

    async def product_by_barcode(self, barcode: str) -> Product | None:
        self._pre(self._fault())
        pid = self.barcodes.get(barcode)
        return None if pid is None else self.products[pid]

    async def external_lookup(self, barcode: str) -> Product | None:
        self._pre(self._fault())
        p = self.lookup.get(barcode)
        if p is not None:
            self.products[p.id] = p
            self.barcodes[barcode] = p.id
        return p

    async def product(self, product_id: int) -> Product:
        self._pre(self._fault())
        if product_id not in self.products:
            raise GrocyRequestError(400, "no such product")
        return self.products[product_id]

    async def add(
        self, product_id: int, *, location_id: int, best_before: str | None, note: str
    ) -> str | None:
        fault = self._fault()
        self._pre(fault)
        if fault == "late":
            self._late.append([2, product_id, location_id, note])
            raise GrocyUnavailable("timed out", maybe_applied=True)
        sid = self._apply_add(product_id, location_id, note)
        if fault == "lost":
            raise GrocyUnavailable("timed out", maybe_applied=True)
        return sid

    async def consume(
        self, product_id: int, *, stock_entry_id: str | None, location_id: int
    ) -> None:
        fault = self._fault()
        self._pre(fault)
        for i, e in enumerate(self.stock):
            if e.product_id == product_id and (
                stock_entry_id is None or e.stock_id == stock_entry_id
            ):
                del self.stock[i]
                self.log.append(("consume", product_id, e.stock_id))
                break
        else:
            raise GrocyRequestError(400, "not in stock")
        if fault == "lost":
            raise GrocyUnavailable("timed out", maybe_applied=True)

    async def entries(self, product_id: int) -> list[StockEntry]:
        fault = self._fault()
        self._pre(fault)
        return [e for e in self.stock if e.product_id == product_id]
