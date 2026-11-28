"""Unload and Stock-take: barcode scanning at home, outside Kroger Restock.

**Unload** (groceries bought in the Kroger app, in a store, elsewhere): each
scan finds or creates the Grocy product (:class:`~.products.ProductResolver`)
and adds 1 to that barcode's open Just bought row (source ``unload``). The
rows then work like Kroger rows: Tag them / Not tagging. Nothing goes to the
Kroger cart (``kroger_restock.scan_upc`` is never called).

**Stock-take** (what's already in the pantry): each scan counts one unit of a
product; quantities can be typed in the web UI. **Finish** builds a review
(product, counted, tags present, Grocy's pantry amount, difference); **Apply**
either sets Grocy to the counts with an inventory correction or adds the
missing units as purchases. Products not counted are left alone unless
"Products I didn't scan are gone" is ticked. After applying, each row offers
**Tag N** (a counted link session); the counted units are remembered as
booked, so a tag linked within 7 days doesn't add them again on its first
arrival read (the Not-tagging rule).

Tagged units belong to RFID presence: a stock-take counts the *untagged*
units; a product's target is ``counted + tags present in the pantry``.

Sessions are started from the web UI or the linking station (triple-click:
Unload, long-press: Stock-take) and end after 3 minutes without a scan or
when stopped. MQTT (prefix ``pantry_hub/linking/<station>/``)::

    scan        ← {"barcode": "...", "mode": "unload"|"stocktake", "ts": ...}
    mode_state  ← {"mode": "kroger"|"link"|"unload"|"stocktake"}
    mode        → {"mode": "unload"|"stocktake"|"off"}
    result      → {"status": "added"|"counted"|"unknown_barcode"|"error", ...}
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
import json
import logging
import time
from typing import Any, Literal

from .barcodes import digits
from .grocy import GrocyClient, GrocyError, Product
from .products import ProductResolver
from .store import Store

_LOGGER = logging.getLogger(__name__)

Mode = Literal["unload", "stocktake"]
MODES: tuple[Mode, ...] = ("unload", "stocktake")
#: A session ends after this long without a scan.
IDLE_S = 180.0
STOCKTAKE_NOTE = "Pantry Hub stock-take"


class ScanError(Exception):
    """A scan or stock-take action can't be done (message for the person)."""


@dataclass(slots=True)
class ScanResult:
    """Answer to one scan (MQTT ``result`` and the web UI toast)."""

    status: Literal["added", "counted", "unknown_barcode", "error"]
    message: str
    product: str = ""
    barcode: str = ""
    mode: str = ""
    quantity: int = 0

    def payload(self) -> dict[str, Any]:
        """JSON-able dict for the station."""
        return {
            "status": self.status,
            "product": self.product,
            "message": self.message,
            "barcode": self.barcode,
            "mode": self.mode,
            "quantity": self.quantity,
        }


@dataclass(slots=True)
class Session:
    """The running Unload / Stock-take session."""

    mode: Mode
    source: str  # "web" or a station id
    started: float  # wall clock
    last: float  # loop time of the last scan / start
    station: str | None = None
    scans: int = 0

    def as_dict(self) -> dict[str, Any]:
        """For ``/api/state``."""
        return {
            "mode": self.mode,
            "source": self.source,
            "station": self.station,
            "started": self.started,
            "scans": self.scans,
        }


@dataclass(slots=True)
class ReviewRow:
    """One product in the stock-take review."""

    product_id: int
    name: str
    counted: int
    tags: int  # linked tags present in the pantry now
    grocy: float  # Grocy's amount at the pantry location
    scanned: bool  # counted in this stock-take (else: only in Grocy)
    tagged_after: int = 0
    applied: str | None = None  # what Apply did ("set", "purchase", "gone", None)
    #: Lowering: units Apply couldn't remove because the pantry had fewer.
    shortfall: float = 0.0
    #: Units Grocy shows in other locations (never touched by Apply).
    elsewhere: float = 0.0

    @property
    def target(self) -> int:
        """Units that should be in the pantry: counted untagged + tags."""
        return self.counted + self.tags

    @property
    def difference(self) -> float:
        """Target minus Grocy (positive: Grocy is missing units)."""
        return self.target - self.grocy

    def as_dict(self) -> dict[str, Any]:
        """For ``/api/state``."""
        return {
            "product_id": self.product_id,
            "name": self.name,
            "counted": self.counted,
            "tags": self.tags,
            "target": self.target,
            "grocy": self.grocy,
            "difference": self.difference,
            "scanned": self.scanned,
            "applied": self.applied,
            "tagged_after": self.tagged_after,
            "to_tag": max(self.counted - self.tagged_after, 0),
            "shortfall": self.shortfall,
            "elsewhere": self.elsewhere,
        }


@dataclass(slots=True)
class StockTakeState:
    """Persisted (meta ``stocktake``): phase + the review once finished."""

    phase: Literal["", "counting", "review", "applied"] = ""
    started: float | None = None
    how: str | None = None
    gone: bool = False
    review: list[ReviewRow] = field(default_factory=list)

    def dump(self) -> str:
        """JSON for the meta table."""
        return json.dumps(
            {
                "phase": self.phase,
                "started": self.started,
                "how": self.how,
                "gone": self.gone,
                "review": [
                    {
                        "product_id": r.product_id,
                        "name": r.name,
                        "counted": r.counted,
                        "tags": r.tags,
                        "grocy": r.grocy,
                        "scanned": r.scanned,
                        "tagged_after": r.tagged_after,
                        "applied": r.applied,
                        "shortfall": r.shortfall,
                        "elsewhere": r.elsewhere,
                    }
                    for r in self.review
                ],
            }
        )

    @classmethod
    def load(cls, raw: str | None) -> StockTakeState:
        """From the meta table (empty state if missing or unreadable)."""
        if not raw:
            return cls()
        try:
            data = json.loads(raw)
            return cls(
                phase=data.get("phase", ""),
                started=data.get("started"),
                how=data.get("how"),
                gone=bool(data.get("gone")),
                review=[ReviewRow(**r) for r in data.get("review", [])],
            )
        except TypeError, ValueError:
            return cls()


PublishFn = Callable[[str, dict[str, Any]], Awaitable[None]]
TagsFn = Callable[[], Awaitable[dict[int, int]]]


def clean_barcode(raw: object) -> str | None:
    """Return a scanned code as digits (4–32), else None."""
    if isinstance(raw, int) and not isinstance(raw, bool):
        raw = str(raw)
    if not isinstance(raw, str):
        return None
    code = digits(raw)
    return code if 4 <= len(code) <= 32 else None


class ScanManager:
    """Unload / Stock-take sessions, scans and the stock-take review."""

    def __init__(
        self,
        store: Store,
        grocy: GrocyClient,
        resolver: ProductResolver,
        *,
        location_id: int,
        tags_present: TagsFn,
        publish_mode: PublishFn | None = None,
        on_unload: Callable[[float], Awaitable[None]] | None = None,
        unload_order: Callable[[float, str], Awaitable[str | None]] | None = None,
        on_change: Callable[[], None] | None = None,
        clock: Callable[[], float] = time.time,
        idle_s: float = IDLE_S,
    ) -> None:
        """Create.

        ``tags_present()`` → linked tags in the pantry per Grocy product.
        ``publish_mode(station, payload)`` → MQTT ``mode`` command.
        ``on_unload(now)`` → an Unload scan was booked (untracked countdown).
        """
        self.store = store
        self.grocy = grocy
        self.resolver = resolver
        self.location_id = location_id
        self.tags_present = tags_present
        self.publish_mode = publish_mode
        self.on_unload = on_unload
        self.unload_order = unload_order
        self.on_change = on_change
        self.clock = clock
        self.idle_s = idle_s
        self.session: Session | None = None
        self.stocktake = StockTakeState()
        #: Station a web-started session is sent to (the station used last).
        self.default_station: Callable[[], str | None] = lambda: None
        self.last_result: ScanResult | None = None

    def _changed(self) -> None:
        if self.on_change is not None:
            self.on_change()

    @staticmethod
    def _now_loop() -> float:
        return asyncio.get_running_loop().time()

    async def load(self) -> None:
        """Restore the stock-take after a restart."""
        self.stocktake = StockTakeState.load(await self.store.get_meta("stocktake"))

    async def _save(self) -> None:
        await self.store.set_meta("stocktake", self.stocktake.dump())

    # Sessions ------------------------------------------------------------------

    async def start(
        self, mode: Mode, *, source: str = "web", station: str | None = None
    ) -> Session:
        """Start (or switch to) a session; tell the station when the web started it."""
        if mode not in MODES:
            raise ScanError(f"unknown mode {mode!r}")
        old = self.session
        if source == "web":
            station = station or self.default_station()
        self.session = Session(
            mode=mode,
            source=source,
            started=self.clock(),
            last=self._now_loop(),
            station=station,
        )
        if mode == "stocktake" and self.stocktake.phase != "counting":
            await self.begin_stocktake()
        if source == "web" and station and self.publish_mode is not None:
            await self.publish_mode(station, {"mode": mode})
        if old is not None and old.station and old.station != station:
            await self._tell_off(old)
        _LOGGER.info("%s started (%s)", mode.capitalize(), source)
        self._changed()
        return self.session

    async def stop(self, reason: str = "stopped", *, tell_station: bool = True) -> None:
        """End the session (station back to Kroger mode)."""
        session, self.session = self.session, None
        if session is None:
            return
        if tell_station:
            await self._tell_off(session)
        _LOGGER.info(
            "%s ended (%s, %d scans)", session.mode.capitalize(), reason, session.scans
        )
        self._changed()

    async def _tell_off(self, session: Session) -> None:
        if session.station and self.publish_mode is not None:
            await self.publish_mode(session.station, {"mode": "off"})

    async def check_idle(self) -> None:
        """End a session idle for :attr:`idle_s` (called from the hub's timer)."""
        s = self.session
        if s is not None and self._now_loop() - s.last >= self.idle_s:
            await self.stop("3 minutes without a scan")

    async def on_station_mode(self, station: str, payload: bytes) -> None:
        """Follow a mode change made at the station (clicks, long-press, idle)."""
        try:
            data = json.loads(payload)
        except UnicodeDecodeError, ValueError:
            return
        mode = data.get("mode") if isinstance(data, dict) else None
        if mode in MODES:
            if self.session is None or self.session.mode != mode:
                await self.start(mode, source=station, station=station)
            else:
                self.session.station = station
        elif self.session is not None and self.session.station == station:
            await self.stop(
                f"station {station} left {self.session.mode}", tell_station=False
            )

    # Scans -----------------------------------------------------------------------

    async def scan(
        self, raw: object, mode: str | None, *, source: str = "web"
    ) -> ScanResult:
        """Handle one Unload / Stock-take scan."""
        mode = mode or (self.session.mode if self.session else "unload")
        if mode not in MODES:
            return ScanResult("error", f"Unknown scan mode {mode!r}")
        barcode = clean_barcode(raw)
        if barcode is None:
            return ScanResult("error", "That isn't a product barcode", mode=mode)
        s = self.session
        if s is None or s.mode != mode:
            station = None if source == "web" else source
            s = await self.start(mode, source=source, station=station)
        s.last = self._now_loop()
        s.scans += 1
        try:
            product = await self.resolver.find_or_create(barcode)
        except GrocyError as err:
            result = ScanResult("error", f"Grocy: {err}", barcode=barcode, mode=mode)
            self.last_result = result
            return result
        if product is None:
            await self.store.add_needs_product(barcode, source, self.clock())
            result = ScanResult(
                "unknown_barcode",
                f"No product found for {barcode}; added to “needs a product”",
                barcode=barcode,
                mode=mode,
            )
        elif mode == "unload":
            result = await self._unload(product, barcode)
        else:
            result = await self._count(product, barcode)
        self.last_result = result
        self._changed()
        return result

    async def _unload(self, product: Product, barcode: str) -> ScanResult:
        now = self.clock()
        order = (
            await self.unload_order(now, barcode)
            if self.unload_order is not None
            else None
        )
        row = await self.store.add_unload_scan(
            barcode=barcode,
            product_id=product.id,
            name=product.name,
            now=now,
            order_ref=order,
        )
        if self.on_unload is not None:
            await self.on_unload(now)
        return ScanResult(
            "added",
            f"Just bought: {product.name} ×{row.quantity}",
            product=product.name,
            barcode=barcode,
            mode="unload",
            quantity=row.quantity,
        )

    async def _count(self, product: Product, barcode: str) -> ScanResult:
        if self.stocktake.phase != "counting":
            await self.begin_stocktake()
        n = await self.store.stocktake_add(
            product.id, product.name, barcode, self.clock()
        )
        return ScanResult(
            "counted",
            f"Stock-take: {product.name} — {n} counted",
            product=product.name,
            barcode=barcode,
            mode="stocktake",
            quantity=n,
        )

    # Stock-take ------------------------------------------------------------------

    async def begin_stocktake(self) -> None:
        """Start counting from zero (a finished or applied one is replaced)."""
        await self.store.stocktake_clear()
        self.stocktake = StockTakeState(phase="counting", started=self.clock())
        await self._save()
        self._changed()

    async def set_count(self, product_id: int, counted: int) -> None:
        """Typed quantity for a counted product."""
        if self.stocktake.phase != "counting":
            raise ScanError("No stock-take is being counted")
        if counted < 0 or counted > 999:
            raise ScanError("Enter a number from 0 to 999")
        if not await self.store.stocktake_set(product_id, counted, self.clock()):
            raise ScanError("Scan the product once first")
        self._changed()

    async def counts(self) -> list[dict[str, Any]]:
        """Return the counted products while counting."""
        return await self.store.stocktake_rows()

    async def _pantry_amounts(self) -> dict[int, float]:
        amounts: dict[int, float] = {}
        for e in await self.grocy.location_entries(self.location_id):
            amounts[e.product_id] = amounts.get(e.product_id, 0.0) + e.amount
        return amounts

    async def finish(self) -> list[ReviewRow]:
        """Build the review: counted products, plus products only Grocy has."""
        if self.stocktake.phase not in ("counting", "review"):
            raise ScanError("Start a stock-take first")
        try:
            pantry = await self._pantry_amounts()
        except GrocyError as err:
            raise ScanError(f"Grocy: {err}") from err
        tags = await self.tags_present()
        rows: list[ReviewRow] = []
        counted_ids: set[int] = set()
        for r in await self.store.stocktake_rows():
            pid = int(r["product_id"])
            counted_ids.add(pid)
            rows.append(
                ReviewRow(
                    product_id=pid,
                    name=str(r["product_name"]),
                    counted=int(r["scanned"]),
                    tags=tags.get(pid, 0),
                    grocy=pantry.get(pid, 0.0),
                    scanned=True,
                    tagged_after=int(r["tagged"]),
                )
            )
        names = await self._names([p for p in pantry if p not in counted_ids])
        for pid, amount in sorted(pantry.items()):
            # Every unit tagged and Grocy agrees: RFID owns it, skip it.
            if pid in counted_ids or amount <= 0 or amount == tags.get(pid, 0):
                continue
            rows.append(
                ReviewRow(
                    product_id=pid,
                    name=names.get(pid, f"Product #{pid}"),
                    counted=0,
                    tags=tags.get(pid, 0),
                    grocy=amount,
                    scanned=False,
                )
            )
        rows.sort(key=lambda r: (not r.scanned, r.name.casefold()))
        self.stocktake.phase = "review"
        self.stocktake.review = rows
        await self._save()
        await self.stop("finished")
        self._changed()
        return rows

    async def _names(self, ids: list[int]) -> dict[int, str]:
        names = {lk.product_id: lk.product_name for lk in await self.store.links()}
        for pid in ids:
            if pid not in names:
                try:
                    names[pid] = (await self.grocy.product(pid)).name
                except GrocyError:
                    continue
        return names

    async def apply(self, how: str, *, gone: bool = False) -> str:
        """Apply the review: ``set`` (inventory correction) or ``purchase``."""
        st = self.stocktake
        if st.phase != "review":
            raise ScanError("Finish the stock-take first")
        if how not in ("set", "purchase"):
            raise ScanError("Choose how to apply the counts")
        now = self.clock()
        changed = 0
        try:
            for row in st.review:
                if row.scanned:
                    if how == "set" and row.difference != 0:
                        await self._set_pantry(row)
                        row.applied = "set"
                        changed += 1
                    elif how == "purchase" and row.difference > 0:
                        await self.grocy.purchase(
                            row.product_id,
                            row.difference,
                            location_id=self.location_id,
                            price=None,
                            note=STOCKTAKE_NOTE,
                        )
                        row.applied = "purchase"
                        changed += 1
                    if row.counted:
                        # Counted units are in Grocy now: a tag linked to one
                        # of them within 7 days must not add it again.
                        await self.store.add_allowance(
                            row.product_id, row.counted, now, "stocktake"
                        )
                elif gone and row.grocy > row.target:
                    await self._set_pantry(row)
                    row.applied = "gone"
                    changed += 1
        except GrocyError as err:
            await self._save()
            raise ScanError(f"Grocy stopped the update: {err}") from err
        st.phase, st.how, st.gone = "applied", how, gone
        await self._save()
        self._changed()
        verb = "Set" if how == "set" else "Added purchases for"
        return f"{verb} {changed} product{'s' if changed != 1 else ''} in Grocy"

    async def _set_pantry(self, row: ReviewRow) -> None:
        """Make the *pantry* hold ``row.target``; other locations are never touched.

        Raising: an inventory correction (new total = total + missing; the
        added units go to the pantry). Lowering: consume from the pantry
        location explicitly (not spoiled); if the pantry holds less than the
        drop, only what's there, and the rest is reported on the row.
        """
        entries = await self.grocy.entries(row.product_id)
        total = sum(e.amount for e in entries)
        pantry = sum(e.amount for e in entries if e.location_id == self.location_id)
        row.elsewhere = total - pantry
        drop = row.grocy - row.target  # what the review promised to remove
        if drop < 0:
            missing = row.target - pantry
            if missing > 0:
                await self.grocy.inventory(
                    row.product_id,
                    total + missing,
                    location_id=self.location_id,
                    note=STOCKTAKE_NOTE,
                )
            return
        take = min(drop, pantry)
        if take > 0:
            await self.grocy.consume_amount(
                row.product_id, take, location_id=self.location_id, note=STOCKTAKE_NOTE
            )
        row.shortfall = drop - take

    def tag_count(self, product_id: int) -> int:
        """Units "Tag N" offers for a product (counted, not yet tagged)."""
        if self.stocktake.phase != "applied":
            raise ScanError("Apply the stock-take before tagging")
        for row in self.stocktake.review:
            if row.product_id == product_id and row.scanned:
                n = row.counted - row.tagged_after
                if n < 1:
                    raise ScanError("Every counted unit is tagged")
                return n
        raise ScanError("That product isn't in the stock-take")

    async def tagged_one(self, product_id: int) -> None:
        """Count one more unit of ``product_id`` linked by a Tag N session."""
        for row in self.stocktake.review:
            if row.product_id == product_id:
                row.tagged_after += 1
        await self.store.stocktake_tagged(product_id)
        await self._save()
        self._changed()

    async def close_stocktake(self) -> None:
        """Cancel (while counting / reviewing) or close (after applying)."""
        if self.session is not None and self.session.mode == "stocktake":
            await self.stop("stock-take closed")
        await self.store.stocktake_clear()
        self.stocktake = StockTakeState()
        await self._save()
        self._changed()

    async def state(self) -> dict[str, Any]:
        """For ``/api/state``."""
        st = self.stocktake
        return {
            "phase": st.phase,
            "started": st.started,
            "how": st.how,
            "gone": st.gone,
            "counts": await self.counts() if st.phase == "counting" else [],
            "review": [r.as_dict() for r in st.review],
        }
