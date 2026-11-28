"""Find or create the Grocy product for a scanned barcode.

Shared by Just bought (Kroger deliveries), Unload and Stock-take:

1. Grocy by barcode, in every form of the code (printed, UPC-A / EAN-13
   twin, Kroger's 13-digit form; see :mod:`pantry_hub.barcodes`).
2. Otherwise create it from ``kroger_restock.lookup_product`` (name, brand ·
   size, then the picture through the normal picture flow).
3. Otherwise Grocy's external barcode lookup plugin (creates the product).
4. Otherwise ``None``: the caller adds the barcode to "needs product".
"""

from __future__ import annotations

import logging
from typing import Any

from .barcodes import variants
from .grocy import GrocyClient, Product
from .pictures import PictureError, PictureService

_LOGGER = logging.getLogger(__name__)


def _text(value: object, limit: int = 200) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()[:limit]
    return None


class ProductResolver:
    """Barcode → Grocy product, creating it when Kroger or Grocy know it."""

    def __init__(
        self, grocy: GrocyClient, pictures: PictureService, *, location_id: int
    ) -> None:
        """Create."""
        self.grocy = grocy
        self.pictures = pictures
        self.location_id = location_id

    async def find(self, barcode: str) -> Product | None:
        """Grocy product with this barcode in any of its forms."""
        for code in variants(barcode):
            product: Product | None = await self.grocy.product_by_barcode(code)
            if product is not None:
                return product
        return None

    async def kroger_info(self, barcode: str) -> dict[str, Any] | None:
        """Kroger Restock's lookup (None if missing, not found or failing)."""
        try:
            info = await self.pictures.lookup(barcode)
        except PictureError as err:  # the lookup only improves the product
            _LOGGER.info("Kroger lookup for %s failed: %s", barcode, err)
            return None
        return info if info and info.get("found") else None

    async def create(
        self,
        barcode: str,
        *,
        name: str | None = None,
        brand: str | None = None,
        size: str | None = None,
        info: dict[str, Any] | None = None,
    ) -> Product:
        """Create a Grocy product with the printed ``barcode``.

        Kroger's ``info`` (when given) wins for name, brand and size; the
        ``name`` / ``brand`` / ``size`` hints (e.g. from a delivery) fill in.
        """
        if info:
            name = _text(info.get("name")) or name
            brand = _text(info.get("brand")) or brand
            size = _text(info.get("size"), 64) or size
        description = " · ".join(v for v in (brand, size) if v)
        product: Product = await self.grocy.create_product(
            name or barcode,
            barcode=barcode,
            location_id=self.location_id,
            description=description,
        )
        _LOGGER.info("Created Grocy product %r (%s)", product.name, barcode)
        self.pictures.on_link(product.id, barcode)
        return product

    async def find_or_create(self, barcode: str) -> Product | None:
        """Find or create (steps 1–3 above); ``None`` means it needs a product."""
        product = await self.find(barcode)
        if product is not None:
            return product
        if self.pictures.blocked() is None:
            info = await self.kroger_info(barcode)
            if info is not None:
                return await self.create(barcode, info=info)
        product = await self.grocy.external_lookup(barcode)
        if product is not None:
            _LOGGER.info(
                "Created Grocy product %r from the barcode lookup plugin", product.name
            )
            self.pictures.on_link(product.id, barcode)
        return product
