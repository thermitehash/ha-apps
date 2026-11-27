"""Small Grocy REST client (swappable: the hub only uses :class:`StockBackend`).

Endpoints used (checked against Grocy v4.7.1's ``grocy.openapi.json``):

* ``GET  /api/system/info`` — validate URL + API key at start
* ``GET  /api/objects/locations/{id}`` — validate the pantry location
* ``GET  /api/stock/products/by-barcode/{barcode}`` — linking lookup
* ``GET  /api/stock/barcodes/external-lookup/{barcode}?add=true`` — lookup plugin
* ``GET  /api/stock/products/{id}`` — product name / default best-before days
* ``GET  /api/stock/products/{id}/entries`` — idempotency checks, resync
* ``POST /api/stock/products/{id}/add`` — arrival
* ``POST /api/stock/products/{id}/consume`` — departure
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import logging
from typing import Any, Protocol
from urllib.parse import quote

import aiohttp

_LOGGER = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 15.0


class GrocyError(Exception):
    """Base class for Grocy failures."""


class GrocyUnavailable(GrocyError):
    """Transient: connection error, timeout or 5xx. Retry later.

    ``maybe_applied`` is True when the request may have reached Grocy (so a
    retry must first check whether it already took effect).
    """

    def __init__(self, message: str, *, maybe_applied: bool) -> None:
        """Create."""
        super().__init__(message)
        self.maybe_applied = maybe_applied


class GrocyAuthError(GrocyError):
    """401/403: the API key is wrong or lacks permission."""


class GrocyRequestError(GrocyError):
    """4xx other than auth: Grocy rejected the request. Retrying will not help."""

    def __init__(self, status: int, message: str) -> None:
        """Create."""
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


@dataclass(frozen=True, slots=True)
class Product:
    """The bits of a Grocy product the hub needs."""

    id: int
    name: str
    default_best_before_days: int | None = None


@dataclass(frozen=True, slots=True)
class StockEntry:
    """One Grocy stock entry (a unit or batch)."""

    stock_id: str
    product_id: int
    amount: float
    location_id: int | None
    note: str


class StockBackend(Protocol):
    """What the hub needs from an inventory system."""

    async def product_by_barcode(self, barcode: str) -> Product | None:
        """Return the product with ``barcode``, or None if unknown."""

    async def external_lookup(self, barcode: str) -> Product | None:
        """Ask the lookup plugin; create the product if found."""

    async def product(self, product_id: int) -> Product:
        """Return one product."""

    async def add(
        self,
        product_id: int,
        *,
        location_id: int,
        best_before: str | None,
        note: str,
    ) -> str | None:
        """Add one unit; return the new stock entry id."""

    async def consume(
        self, product_id: int, *, stock_entry_id: str | None, location_id: int
    ) -> None:
        """Consume one unit."""

    async def entries(self, product_id: int) -> list[StockEntry]:
        """Return the product's current stock entries."""


def _message(body: Any) -> str:
    if isinstance(body, dict) and "error_message" in body:
        return str(body["error_message"])
    return str(body)[:200]


class GrocyClient:
    """aiohttp-based :class:`StockBackend`."""

    def __init__(
        self,
        url: str,
        api_key: str,
        *,
        session: aiohttp.ClientSession | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        """Create; call :meth:`close` when done if no session was passed."""
        self.base = url.rstrip("/") + "/api"
        self._api_key = api_key
        self._own_session = session is None
        self._session = session
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        #: Set after every request: True on any HTTP answer, False on no answer.
        self.reachable: bool | None = None

    async def close(self) -> None:
        """Close the session if we own it."""
        if self._own_session and self._session is not None:
            await self._session.close()
            self._session = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
    ) -> Any:
        url = self.base + path
        headers = {"GROCY-API-KEY": self._api_key, "Accept": "application/json"}
        mutating = method != "GET"
        try:
            async with self._get_session().request(
                method,
                url,
                json=json,
                params=params,
                headers=headers,
                timeout=self._timeout,
            ) as resp:
                self.reachable = True
                text = await resp.text()
                body: Any = None
                if text:
                    try:
                        body = await resp.json(content_type=None)
                    except ValueError:
                        body = text
                if resp.status in (401, 403):
                    raise GrocyAuthError(
                        f"Grocy refused the API key (HTTP {resp.status})"
                    )
                if resp.status >= 500:
                    raise GrocyUnavailable(
                        f"Grocy HTTP {resp.status} on {method} {path}: {_message(body)}",
                        maybe_applied=mutating,
                    )
                if resp.status >= 400:
                    raise GrocyRequestError(resp.status, _message(body))
                return body
        except aiohttp.ClientConnectorError as err:
            self.reachable = False
            raise GrocyUnavailable(
                f"cannot connect to Grocy at {self.base}: {err}", maybe_applied=False
            ) from err
        except TimeoutError as err:
            self.reachable = False
            raise GrocyUnavailable(
                f"Grocy timed out on {method} {path}", maybe_applied=mutating
            ) from err
        except aiohttp.ClientError as err:
            self.reachable = False
            raise GrocyUnavailable(
                f"Grocy request {method} {path} failed: {err!r}",
                maybe_applied=mutating,
            ) from err

    # Validation ----------------------------------------------------------------

    async def system_info(self) -> dict[str, Any]:
        """Return ``/system/info`` (validates URL and key)."""
        body = await self._request("GET", "/system/info")
        if not isinstance(body, dict):
            raise GrocyError(
                f"{self.base}/system/info did not return Grocy system info. "
                "Is grocy_url pointing at Grocy?"
            )
        return body

    async def location_name(self, location_id: int) -> str:
        """Return the name of location ``location_id``."""
        body = await self._request("GET", f"/objects/locations/{location_id}")
        if not isinstance(body, dict) or "name" not in body:
            raise GrocyRequestError(404, f"location {location_id} not found")
        return str(body["name"])

    # Products ------------------------------------------------------------------

    @staticmethod
    def _product(data: Any) -> Product:
        if not isinstance(data, dict):
            raise GrocyError(f"unexpected product payload: {data!r}")
        days = data.get("default_best_before_days")
        return Product(
            id=int(data["id"]),
            name=str(data.get("name", f"Product {data['id']}")),
            default_best_before_days=None if days is None else int(days),
        )

    async def product_by_barcode(self, barcode: str) -> Product | None:
        """Return the product with ``barcode``; None if Grocy does not know it."""
        try:
            body = await self._request(
                "GET", f"/stock/products/by-barcode/{quote(barcode, safe='')}"
            )
        except GrocyRequestError as err:
            if err.status in (400, 404):
                return None
            raise
        if not isinstance(body, dict):
            raise GrocyError(f"unexpected by-barcode payload: {body!r}")
        return self._product(body.get("product"))

    async def external_lookup(self, barcode: str) -> Product | None:
        """Run Grocy's barcode lookup plugin with ``add=true``.

        Returns the newly created product, or None when the plugin finds
        nothing, is disabled or fails.
        """
        try:
            body = await self._request(
                "GET",
                f"/stock/barcodes/external-lookup/{quote(barcode, safe='')}",
                params={"add": "true"},
            )
        except GrocyRequestError as err:
            _LOGGER.info(
                "Barcode lookup plugin gave no product for %s: %s", barcode, err
            )
            return None
        if not isinstance(body, dict) or body.get("id") is None:
            return None
        product = await self.product(int(body["id"]))
        return product

    async def product(self, product_id: int) -> Product:
        """Return product ``product_id``."""
        body = await self._request("GET", f"/stock/products/{product_id}")
        if not isinstance(body, dict):
            raise GrocyError(f"unexpected product details payload: {body!r}")
        return self._product(body.get("product"))

    async def product_picture(
        self, product_id: int, size: int = 128
    ) -> tuple[bytes, str] | None:
        """Return ``(image bytes, content type)`` of the product's picture, or None.

        Grocy serves pictures from ``/files/productpictures/<base64 name>``;
        ``best_fit_*`` asks it for a thumbnail.
        """
        body = await self._request("GET", f"/stock/products/{product_id}")
        product = body.get("product") if isinstance(body, dict) else None
        name = product.get("picture_file_name") if isinstance(product, dict) else None
        if not isinstance(name, str) or not name:
            return None
        encoded = base64.b64encode(name.encode()).decode()
        url = f"{self.base}/files/productpictures/{encoded}"
        params = {
            "force_serve_as": "picture",
            "best_fit_width": str(size),
            "best_fit_height": str(size),
        }
        headers = {"GROCY-API-KEY": self._api_key}
        try:
            async with self._get_session().get(
                url, params=params, headers=headers, timeout=self._timeout
            ) as resp:
                if resp.status != 200:
                    return None
                ctype = resp.content_type or "application/octet-stream"
                if not ctype.startswith("image/"):
                    return None
                return await resp.read(), ctype
        except (aiohttp.ClientError, TimeoutError) as err:
            raise GrocyUnavailable(
                f"Grocy picture for product {product_id}: {err!r}", maybe_applied=False
            ) from err

    # Stock ---------------------------------------------------------------------

    async def add(
        self,
        product_id: int,
        *,
        location_id: int,
        best_before: str | None,
        note: str,
    ) -> str | None:
        """Add one unit and return its stock entry id (``stock_id``)."""
        payload: dict[str, Any] = {
            "amount": 1,
            "transaction_type": "purchase",
            "location_id": location_id,
            "note": note,
        }
        if best_before:
            payload["best_before_date"] = best_before
        body = await self._request(
            "POST", f"/stock/products/{product_id}/add", json=payload
        )
        if isinstance(body, list):
            for entry in body:
                if isinstance(entry, dict) and entry.get("stock_id"):
                    return str(entry["stock_id"])
        _LOGGER.warning("Grocy add for product %s returned no stock_id", product_id)
        return None

    async def consume(
        self, product_id: int, *, stock_entry_id: str | None, location_id: int
    ) -> None:
        """Consume one unit (that exact unit when ``stock_entry_id`` is known)."""
        payload: dict[str, Any] = {"amount": 1, "transaction_type": "consume"}
        if stock_entry_id:
            payload["stock_entry_id"] = stock_entry_id
        else:
            payload["location_id"] = location_id
        await self._request(
            "POST", f"/stock/products/{product_id}/consume", json=payload
        )

    async def entries(self, product_id: int) -> list[StockEntry]:
        """Return current stock entries of ``product_id``."""
        body = await self._request("GET", f"/stock/products/{product_id}/entries")
        if not isinstance(body, list):
            raise GrocyError(f"unexpected entries payload: {body!r}")
        out: list[StockEntry] = []
        for item in body:
            if not isinstance(item, dict):
                continue
            loc = item.get("location_id")
            out.append(
                StockEntry(
                    stock_id=str(item.get("stock_id", "")),
                    product_id=int(item.get("product_id", product_id)),
                    amount=float(item.get("amount", 0)),
                    location_id=None if loc is None else int(loc),
                    note=str(item.get("note") or ""),
                )
            )
        return out
