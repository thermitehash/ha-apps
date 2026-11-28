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

v0.17.3 adds untracked items:

* Event ``kroger_restock_untracked_items`` ``{order_ref, untracked_count,
  pickup_window, received_at, config_entry_id}``, once per order at pickup,
  only when ``untracked_count > 0``.
* ``recent_received`` also returns ``untracked_orders`` (same shape per
  order) and ``untracked_count``.
* Sensor ``sensor.<store>_untracked_items_in_last_order`` (state: the count;
  attributes ``order_ref, item_count, tracked_units, pickup_window, status,
  received_at``).

``FakeKroger(ha, version="0.17.2")`` leaves all of that out.
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

    def __init__(self, ha: FakeHomeAssistant, version: str = "0.17.3") -> None:
        self.ha = ha
        self.version = version
        #: v0.17.3: orders with untracked items (recent_received).
        self.untracked_orders: list[dict[str, Any]] = []
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
        out: dict[str, Any] = {"days": self.days, "items": list(self.recent)}
        if self.version >= "0.17.3":
            out["untracked_orders"] = list(self.untracked_orders)
            out["untracked_count"] = sum(
                o["untracked_count"] for o in self.untracked_orders
            )
        return out

    def untracked_order(
        self,
        order_ref: str,
        count: int,
        received_at: str,
        *,
        sensor: str | None = "sensor.kroger_mt_zion_untracked_items_in_last_order",
        item_count: int = 40,
    ) -> dict[str, Any]:
        """Record a pickup with untracked items (catch-up list + sensor)."""
        order = {
            "order_ref": order_ref,
            "untracked_count": count,
            "pickup_window": "Fri 4:00–5:00 PM",
            "received_at": received_at,
            "config_entry_id": CONFIG_ENTRY,
        }
        self.untracked_orders.append(order)
        if sensor:
            self.ha.add_entity(
                sensor,
                str(count),
                order_ref=order_ref,
                item_count=item_count,
                tracked_units=item_count - count,
                pickup_window=order["pickup_window"],
                status="picked_up",
                received_at=received_at,
                friendly_name="Kroger Mt Zion Untracked items in last order",
            )
        return order

    async def fire_untracked(self, order: dict[str, Any]) -> None:
        """The event v0.17.3 fires at pickup."""
        await self.ha.fire_event("kroger_restock_untracked_items", dict(order))


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
