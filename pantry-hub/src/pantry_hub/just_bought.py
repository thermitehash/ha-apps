"""Just bought: Kroger Restock deliveries waiting to be tagged.

Kroger Restock fires ``kroger_restock_received`` when an order item arrives;
Pantry Hub subscribes over the Supervisor websocket and, on every
(re)connect and at start, catches up with ``kroger_restock.recent_received``
(the last 14 days). Rows are deduplicated on ``item_id`` + ``received_at``.

For each row the person either:

* **Tags them**: a counted link session starts on the linking station (one
  tag per tap, up to the untagged quantity). Tagged units reach Grocy stock
  only through the normal pantry arrival.
* **Not tagging**: the row goes; with "Add to Grocy stock" (default) the
  untagged units are booked as a Grocy purchase (quantity + price). If one of
  them is tagged within 7 days, its arrival is matched against that booking
  (see :mod:`pantry_hub.linking`), so nothing is counted twice.

Rows older than 14 days are shown under "Older"; rows are deleted after 30
days. Nothing is ever booked automatically.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
import logging
import time
from typing import Any

from .barcodes import grocy_barcode, variants
from .grocy import GrocyClient, GrocyError, Product
from .ha_api import HomeAssistantApi, HomeAssistantApiError
from .pictures import DOMAIN, PictureError, PictureService
from .store import Link, Received, Store

_LOGGER = logging.getLogger(__name__)

EVENT_TYPE = "kroger_restock_received"
RECENT_SERVICE = "recent_received"
OLDER_AFTER_S = 14 * 86400.0
PURGE_AFTER_S = 30 * 86400.0
MAX_QUANTITY = 99


class JustBoughtError(Exception):
    """An action on a row could not be done (message is for the person)."""


def _text(value: object, limit: int = 200) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()[:limit]
    if isinstance(value, int | float) and not isinstance(value, bool):
        return str(value)
    return None


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def received_epoch(received_at: str, fallback: float) -> float:
    """``received_at`` (ISO 8601) → epoch seconds; ``fallback`` if unparsable."""
    try:
        parsed = datetime.fromisoformat(received_at.replace("Z", "+00:00"))
    except ValueError:
        return fallback
    if parsed.tzinfo is None:
        return fallback
    return parsed.timestamp()


@dataclass(frozen=True, slots=True)
class Item:
    """A validated ``kroger_restock_received`` payload."""

    item_id: str
    received_at: str
    #: Printed barcode with check digit: what Grocy stores and matches.
    barcode: str
    #: Kroger's 13-digit code without check digit (reference only).
    upc: str
    name: str
    quantity: int
    kroger_product_id: str | None
    brand: str | None
    size: str | None
    image_url: str | None
    price: float | None
    order_ref: str | None

    @classmethod
    def parse(cls, data: dict[str, Any]) -> Item | None:
        """Validate an event / catch-up entry (None if unusable)."""
        item_id = _text(data.get("item_id"), 100)
        received_at = _text(data.get("received_at"), 40)
        upc = _text(data.get("upc"), 64) or ""
        # Kroger's upc has no check digit, so Grocy wouldn't find it: use the
        # printed `barcode`, else rebuild the check digit from `upc`.
        barcode = grocy_barcode(_text(data.get("barcode"), 64), upc)
        quantity = _number(data.get("quantity_received"))
        if not item_id or not received_at or not barcode or quantity is None:
            return None
        qty = int(quantity)
        if qty < 1:
            return None
        return cls(
            item_id=item_id,
            received_at=received_at,
            barcode=barcode,
            upc=upc,
            name=_text(data.get("name")) or barcode,
            quantity=min(qty, MAX_QUANTITY),
            kroger_product_id=_text(data.get("product_id"), 64),
            brand=_text(data.get("brand")),
            size=_text(data.get("size"), 64),
            image_url=_text(data.get("image_url"), 2000),
            price=_number(data.get("price")),
            order_ref=_text(data.get("order_ref"), 100),
        )


StartFn = Callable[[str, Product, Received], Awaitable[None]]


class JustBought:
    """Stores Kroger deliveries and acts on them."""

    def __init__(
        self,
        store: Store,
        grocy: GrocyClient,
        ha: HomeAssistantApi | None,
        pictures: PictureService,
        *,
        location_id: int,
        clock: Callable[[], float] = time.time,
        on_change: Callable[[], None] | None = None,
    ) -> None:
        """Create."""
        self.store = store
        self.grocy = grocy
        self.ha = ha
        self.pictures = pictures
        self.location_id = location_id
        self.clock = clock
        self.on_change = on_change
        #: Whether ``kroger_restock.recent_received`` exists (None: not asked).
        self.available: bool | None = None

    def _changed(self) -> None:
        if self.on_change is not None:
            self.on_change()

    # Intake ----------------------------------------------------------------------

    async def on_event(self, data: dict[str, Any]) -> None:
        """Handle one ``kroger_restock_received`` event."""
        if self.available is not True:
            self.available = True  # an event proves the integration is there
        if await self.ingest(data):
            self._changed()

    async def ingest(self, data: dict[str, Any]) -> bool:
        """Store one item; False if invalid, a duplicate or already expired."""
        item = Item.parse(data)
        if item is None:
            _LOGGER.warning("Ignoring a malformed %s payload", EVENT_TYPE)
            return False
        now = self.clock()
        stored_at = min(received_epoch(item.received_at, now), now)
        if now - stored_at >= PURGE_AFTER_S:
            return False
        row_id = await self.store.add_received(
            item_id=item.item_id,
            received_at=item.received_at,
            upc=item.upc,
            barcode=item.barcode,
            name=item.name,
            quantity=item.quantity,
            now=stored_at,
            kroger_product_id=item.kroger_product_id,
            brand=item.brand,
            size=item.size,
            image_url=item.image_url,
            price=item.price,
            order_ref=item.order_ref,
        )
        if row_id is not None:
            _LOGGER.info("Just bought: %s ×%d", item.name, item.quantity)
        return row_id is not None

    async def catch_up(self) -> int:
        """Fetch ``kroger_restock.recent_received``; return how many rows were new."""
        if self.ha is None:
            self.available = False
            return 0
        found = await self.ha.has_service(DOMAIN, RECENT_SERVICE)
        if found != self.available:
            self.available = found
            self._changed()
        if not found:
            return 0
        try:
            response = await self.ha.call(DOMAIN, RECENT_SERVICE, {})
        except HomeAssistantApiError as err:
            _LOGGER.warning("Just bought catch-up failed: %s", err)
            return 0
        items = response.get("items")
        if items is None:
            items = response.get("received")
        new = 0
        for entry in items if isinstance(items, list) else []:
            if isinstance(entry, dict) and await self.ingest(entry):
                new += 1
        if new:
            _LOGGER.info("Just bought: caught up %d item(s)", new)
            self._changed()
        return new

    async def housekeeping(self) -> int:
        """Delete rows older than 30 days."""
        n = await self.store.purge_received(self.clock() - PURGE_AFTER_S)
        if n:
            self._changed()
        return n

    # Rows ----------------------------------------------------------------------

    async def rows(self) -> list[dict[str, Any]]:
        """Open rows for the web UI, newest first."""
        now = self.clock()
        out = []
        for r in await self.store.received():
            out.append(
                {
                    "id": r.id,
                    "name": r.name,
                    "brand": r.brand,
                    "size": r.size,
                    "quantity": r.quantity,
                    "tagged": r.tagged,
                    "to_tag": r.to_tag,
                    "price": r.price,
                    "received_at": r.received_at,
                    "product_id": r.grocy_product_id,
                    "older": now - r.stored_at >= OLDER_AFTER_S,
                }
            )
        return out

    async def _row(self, row_id: int) -> Received:
        row = await self.store.get_received(row_id)
        if row is None or row.done_at is not None:
            raise JustBoughtError("That item is no longer in Just bought")
        return row

    async def product_for(self, row: Received) -> Product:
        """Grocy product for a row: by barcode, else created from the lookup."""
        if row.grocy_product_id is not None:
            try:
                return await self.grocy.product(row.grocy_product_id)
            except GrocyError:
                pass  # deleted in Grocy: look it up again
        try:
            product = await self._find(row.grocy_barcode)
            if product is None:
                product = await self._create(row)
        except GrocyError as err:
            raise JustBoughtError(f"Grocy: {err}") from err
        await self.store.set_received_product(row.id, product.id)
        return product

    async def _find(self, barcode: str) -> Product | None:
        """Grocy product with this barcode in any of its forms.

        The printed code, its UPC-A / EAN-13 twin and Kroger's 13-digit
        zero-padded form are all tried before a new product is created, so a
        product stored either way is found and not duplicated.
        """
        for code in variants(barcode):
            product: Product | None = await self.grocy.product_by_barcode(code)
            if product is not None:
                return product
        return None

    async def _create(self, row: Received) -> Product:
        name, brand, size = row.name, row.brand, row.size
        barcode = row.grocy_barcode
        try:
            info = await self.pictures.lookup(barcode)
        except PictureError as err:  # the lookup only improves the name
            _LOGGER.info("Product lookup for a new Grocy product failed: %s", err)
            info = None
        if info and info.get("found"):
            name = _text(info.get("name")) or name
            brand = _text(info.get("brand")) or brand
            size = _text(info.get("size"), 64) or size
        description = " · ".join(v for v in (brand, size) if v)
        product: Product = await self.grocy.create_product(
            name,
            barcode=barcode,  # printed form, with check digit
            location_id=self.location_id,
            description=description,
        )
        _LOGGER.info("Created Grocy product %r for a Kroger delivery", name)
        self.pictures.on_link(product.id, barcode)
        return product

    async def tag(self, row_id: int, start: StartFn) -> Product:
        """Tag them: resolve the product and start a counted session."""
        row = await self._row(row_id)
        if row.to_tag < 1:
            raise JustBoughtError("Every unit is already tagged")
        product = await self.product_for(row)
        row = await self._row(row_id)
        await start(row.grocy_barcode, product, row)
        return product

    async def tagged_one(self, row_id: int, link: Link) -> None:
        """Count a unit of this row linked by its counted session."""
        await self.store.add_received_tagged(row_id, self.clock())
        self._changed()

    async def not_tagging(self, row_id: int, *, add_stock: bool) -> str:
        """Not tagging: remove the row; optionally book the units in Grocy."""
        row = await self._row(row_id)
        units = row.to_tag
        if not add_stock or units == 0:
            await self.store.close_received(row.id, self.clock())
            self._changed()
            return f"Removed {row.name}"
        product = await self.product_for(row)
        # Close first so a double click books once; reopen if Grocy refuses.
        units = await self.store.close_received(row.id, self.clock())
        if units == 0:
            raise JustBoughtError("That item is no longer in Just bought")
        try:
            await self.grocy.purchase(
                product.id,
                units,
                location_id=self.location_id,
                price=row.price,
                note=f"Pantry Hub: Kroger delivery {row.order_ref or row.item_id}",
            )
        except GrocyError as err:
            await self.store.reopen_received(row.id)
            raise JustBoughtError(f"Grocy refused the purchase: {err}") from err
        await self.store.add_booked_credit(product.id, units, self.clock())
        _LOGGER.info(
            "Booked %d × %s in Grocy without tags (matched if tagged within 7 days)",
            units,
            product.name,
        )
        self._changed()
        return f"Added {units} × {product.name} to Grocy stock"
