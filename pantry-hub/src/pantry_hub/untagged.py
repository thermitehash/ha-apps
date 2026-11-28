"""Grocy stock in the pantry that has no tags ("In Grocy, not tagged").

Products Heather entered in Grocy by hand (or booked without tags) are listed
on the Tags page with **Tag them**: a counted link session whose links are
"already in stock", so tagging existing Grocy units never adds them again.

Untagged = Grocy's amount at the pantry location − linked tags present.
Read from ``GET /api/stock/locations/<id>/entries``; cached for a minute so a
3-second page poll doesn't hit Grocy each time.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import logging
import time
from typing import Any

from .grocy import GrocyClient, GrocyError

_LOGGER = logging.getLogger(__name__)

TTL_S = 60.0

TagsFn = Callable[[], Awaitable[dict[int, int]]]
NamesFn = Callable[[list[int]], Awaitable[dict[int, str]]]


class GrocyUntagged:
    """Cached list of pantry products with more Grocy units than tags."""

    def __init__(
        self,
        grocy: GrocyClient,
        *,
        location_id: int,
        tags_present: TagsFn,
        names: NamesFn,
        monotonic: Callable[[], float] = time.monotonic,
        ttl_s: float = TTL_S,
    ) -> None:
        """Create."""
        self.grocy = grocy
        self.location_id = location_id
        self.tags_present = tags_present
        self.names = names
        self.monotonic = monotonic
        self.ttl_s = ttl_s
        self.rows: list[dict[str, Any]] = []
        self.error: str | None = None
        self._at: float | None = None
        self._task: asyncio.Task[list[dict[str, Any]]] | None = None

    def stale(self) -> bool:
        """Whether the cached rows are older than the TTL (or missing)."""
        return self._at is None or self.monotonic() - self._at >= self.ttl_s

    def invalidate(self) -> None:
        """Force a refresh on the next read (after tagging, a stock-take…)."""
        self._at = None

    def refresh_soon(self) -> None:
        """Refresh in the background if stale (the page shows the old rows)."""
        if self.stale() and (self._task is None or self._task.done()):
            self._task = asyncio.create_task(
                self.refresh(), name="pantry_hub.grocy_untagged"
            )

    async def refresh(self) -> list[dict[str, Any]]:
        """Read Grocy now."""
        try:
            entries = await self.grocy.location_entries(self.location_id)
        except GrocyError as err:
            self.error = str(err)
            self._at = self.monotonic()
            return self.rows
        amounts: dict[int, float] = {}
        for e in entries:
            amounts[e.product_id] = amounts.get(e.product_id, 0.0) + e.amount
        tags = await self.tags_present()
        names = await self.names(list(amounts))
        rows = []
        for pid, amount in amounts.items():
            untagged = int(amount) - tags.get(pid, 0)
            if untagged > 0:
                rows.append(
                    {
                        "product_id": pid,
                        "name": names.get(pid, f"Product #{pid}"),
                        "in_grocy": amount,
                        "tagged": tags.get(pid, 0),
                        "untagged": untagged,
                    }
                )
        rows.sort(key=lambda r: str(r["name"]).casefold())
        self.rows, self.error, self._at = rows, None, self.monotonic()
        return rows

    def count(self, product_id: int) -> int:
        """Untagged units of a product (from the last read)."""
        for row in self.rows:
            if row["product_id"] == product_id:
                return int(row["untagged"])
        return 0

    async def close(self) -> None:
        """Wait for a running refresh to finish."""
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
