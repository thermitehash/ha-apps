"""Kroger Restock v0.17.2's actions, as verified live in Home Assistant.

* ``kroger_restock.lookup_product`` takes ONLY ``upc`` or ``product_id``
  (mutually exclusive) plus an optional ``config_entry_id``; anything else
  (e.g. ``barcode``) is an HTTP 400. It normalizes UPC-A, EAN-13, UPC-E and
  padding itself. The answer has ``found, query, cached, product_id, upc,
  barcode, name, brand, size, aisle, price, stock_level, images, image_url``,
  and ``reason`` when ``found`` is false. ``upc`` / ``product_id`` are
  Kroger's 13-digit form WITHOUT the check digit; ``barcode`` is the printed
  code WITH it.
* ``kroger_restock.recent_received`` → ``{"days": N, "items": [...]}``.
* Items / ``kroger_restock_received`` events: ``item_id, upc, barcode,
  product_id, name, brand, size, image_url, quantity_received, price,
  received_at, order_ref, config_entry_id``; ``price`` is per unit.
"""

from __future__ import annotations

from typing import Any

from aiohttp import web

from pantry_hub.barcodes import digits, has_valid_check

from .fake_ha import FakeHomeAssistant

CONFIG_ENTRY = "01JKROGERRESTOCKENTRY0000"


def kroger13(code: str) -> str:
    """Any form of a product code → Kroger's 13-digit form (no check digit)."""
    d = digits(code)
    body = d[:-1] if len(d) in (12, 13) and has_valid_check(d) else d
    return body.lstrip("0").zfill(13)


class FakeKroger:
    """Registers Kroger Restock's actions on a :class:`FakeHomeAssistant`."""

    def __init__(self, ha: FakeHomeAssistant) -> None:
        self.ha = ha
        #: Kroger 13-digit code → product fields.
        self.products: dict[str, dict[str, Any]] = {}
        #: Kroger 13-digit code → ``reason`` of a found:false answer.
        self.reasons: dict[str, str] = {}
        #: Reason for every lookup (rate_limited / auth_failed), if set.
        self.all_reason: str | None = None
        self.recent: list[dict[str, Any]] = []
        self.days = 14
        ha.add_action("kroger_restock", "lookup_product", self._lookup)
        ha.add_action("kroger_restock", "recent_received", self._recent)

    def add(self, barcode: str, **fields: Any) -> dict[str, Any]:
        """Add a product Kroger knows, by its printed barcode."""
        key = kroger13(barcode)
        product = {
            "product_id": key,
            "upc": key,
            "barcode": barcode,
            "name": "Kroger product",
            "brand": None,
            "size": None,
            "aisle": "Aisle 7",
            "price": 1.29,
            "stock_level": "HIGH",
            "images": [],
            "image_url": None,
            **fields,
        }
        self.products[key] = product
        return product

    def lookups(self) -> list[dict[str, Any]]:
        return self.ha.rest("kroger_restock", "lookup_product")

    async def _lookup(self, data: dict[str, Any]) -> dict[str, Any]:
        keys = set(data) - {"config_entry_id"}
        if keys not in ({"upc"}, {"product_id"}):
            raise web.HTTPBadRequest(
                text="extra keys not allowed @ data['barcode']"
                if "barcode" in keys
                else "must contain exactly one of upc, product_id"
            )
        query = str(data.get("upc") or data.get("product_id"))
        key = kroger13(query)
        reason = self.all_reason or self.reasons.get(key)
        product = self.products.get(key)
        if reason or product is None:
            return {"found": False, "query": query, "reason": reason or "not_found"}
        return {"found": True, "query": query, "cached": False, **product}

    async def _recent(self, data: dict[str, Any]) -> dict[str, Any]:
        return {"days": self.days, "items": list(self.recent)}


def item(
    *,
    item_id: str,
    barcode: str,
    received_at: str,
    name: str = "Kroger Black Beans",
    quantity: int = 3,
    price: float | None = 0.89,
    with_barcode: bool = True,
    **over: Any,
) -> dict[str, Any]:
    """A ``kroger_restock_received`` event / recent_received item."""
    key = kroger13(barcode)
    data: dict[str, Any] = {
        "item_id": item_id,
        "upc": key,
        "barcode": barcode,
        "product_id": key,
        "name": name,
        "brand": "Kroger",
        "size": "15 oz",
        "image_url": None,
        "quantity_received": quantity,
        "price": price,
        "received_at": received_at,
        "order_ref": "ORD-1",
        "config_entry_id": CONFIG_ENTRY,
    }
    if not with_barcode:
        del data["barcode"]
    data.update(over)
    return data
