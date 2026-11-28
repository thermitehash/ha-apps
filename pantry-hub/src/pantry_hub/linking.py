"""Linking workflow: barcode + tag from a linking station → EPC ↔ product link.

MQTT topics (prefix ``pantry_hub/``)::

    linking/<station_id>/barcode  ← {"barcode": "...", "ts": ...}
    linking/<station_id>/tag      ← {"epc": "...", "rssi": -45.2, "ts": ...}
    linking/<station_id>/result   → {"status": "...", "product": "...", "message": "..."}
    linking/<station_id>/start    → {"barcode": "...", "count": 3}   (counted session)

Either message may carry ``"in_stock": true``: Grocy already counts this
unit, so the add of its current (or next) pantry arrival is skipped.

``status`` is one of ``linked``, ``unknown_barcode``, ``tag_already_linked``,
``timeout`` or ``error``.

Linking never changes Grocy stock: if the item then goes into the pantry, the
normal arrival flow adds it (no double counting). One exception keeps that
true: a unit booked in Grocy without a tag ("Just bought" → Not tagging) and
then linked within a week is marked "already in stock", so its arrival is
matched against the booked unit instead of adding again.

A *counted* session (Just bought → Tag them) is started by the hub, not by a
barcode scan: the hub publishes ``start`` so the station enters link mode,
then links one tag per tap up to ``count``. Each result then carries
``count``, ``linked`` and ``remaining``; the station keeps scanning while
``remaining`` > 0. The session ends at ``count`` or 60 s after the last tap.
Tags already linked to the product are skipped (not counted).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import contextlib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import json
import logging
import re
import time
from typing import Any, Literal

from .grocy import GrocyAuthError, GrocyError, Product, StockBackend
from .store import Link, Store

_LOGGER = logging.getLogger(__name__)

TAG_TIMEOUT_S = 15.0
#: Counted session: seconds without a tap before it ends.
COUNTED_IDLE_S = 60.0
#: A unit booked without a tag matches a link made within this many seconds.
BOOKED_MATCH_S = 7 * 86400.0
RSSI_WINDOW_S = 1.0
STATION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
EPC_RE = re.compile(r"^(?:[0-9A-F]{4}){1,31}$")
BARCODE_MAX = 64

Status = Literal["linked", "unknown_barcode", "tag_already_linked", "timeout", "error"]


@dataclass(frozen=True, slots=True)
class LinkResult:
    """Payload published on ``linking/<station>/result``."""

    status: Status
    message: str
    product: str = ""
    barcode: str = ""
    epc: str = ""
    #: Counted session only: units to link and units linked so far.
    count: int | None = None
    linked: int = 0

    def payload(self) -> dict[str, Any]:
        """JSON-able dict."""
        out: dict[str, Any] = {
            "status": self.status,
            "product": self.product,
            "message": self.message,
        }
        if self.barcode:
            out["barcode"] = self.barcode
        if self.epc:
            out["epc"] = self.epc
        if self.count is not None:
            out["count"] = self.count
            out["linked"] = self.linked
            out["remaining"] = max(self.count - self.linked, 0)
        return out


@dataclass(slots=True)
class _Session:
    station: str
    barcode: str
    started: float
    tags: dict[str, float | None] = field(default_factory=dict)
    first_tag: asyncio.Event = field(default_factory=asyncio.Event)
    window_opened: float | None = None
    in_stock: bool = False
    task: asyncio.Task[None] | None = None


@dataclass(slots=True)
class _Counted:
    """Counted session: link up to ``count`` tags to one product."""

    station: str
    barcode: str
    product: Product
    count: int
    last_tap: float  # loop time
    on_each: Callable[[Link], Awaitable[None]] | None = None
    linked: int = 0
    skipped: int = 0
    tags: dict[str, float | None] = field(default_factory=dict)
    first_tag: asyncio.Event = field(default_factory=asyncio.Event)
    window_opened: float | None = None
    task: asyncio.Task[None] | None = None
    ended: str = ""

    def progress(self) -> dict[str, Any]:
        """For the web UI: ``2 of 3 linked``."""
        return {
            "station": self.station,
            "barcode": self.barcode,
            "product": self.product.name,
            "count": self.count,
            "linked": self.linked,
            "skipped": self.skipped,
            "text": f"{self.linked} of {self.count} linked",
            "ended": self.ended,
        }


def normalize_epc(raw: object) -> str | None:
    """Upper-case hex EPC (whole 16-bit words), or None if invalid."""
    if not isinstance(raw, str):
        return None
    epc = raw.replace(" ", "").replace(":", "").upper()
    return epc if EPC_RE.fullmatch(epc) else None


def _in_stock(data: dict[str, Any] | None) -> bool:
    """``"in_stock": true`` in a barcode or tag message."""
    return data is not None and data.get("in_stock") is True


def _decode(payload: bytes) -> dict[str, Any] | None:
    try:
        data = json.loads(payload)
    except UnicodeDecodeError, ValueError:
        return None
    return data if isinstance(data, dict) else None


PublishFn = Callable[[str, LinkResult], Awaitable[None]]
LinkedFn = Callable[[Link, int | None], Awaitable[None]]
MatchedFn = Callable[[Link], None]


class LinkingManager:
    """One linking session per station."""

    def __init__(
        self,
        store: Store,
        backend: StockBackend,
        *,
        publish: PublishFn,
        location_id: int,
        on_linked: LinkedFn | None = None,
        on_needs_product: Callable[[], None] | None = None,
        clock: Callable[[], float] = time.time,
        tag_timeout: float = TAG_TIMEOUT_S,
        rssi_window: float = RSSI_WINDOW_S,
        counted_idle: float = COUNTED_IDLE_S,
        on_booked_match: MatchedFn | None = None,
    ) -> None:
        """Create."""
        self.store = store
        self.backend = backend
        self.publish = publish
        self.location_id = location_id
        self.on_linked = on_linked
        self.on_needs_product = on_needs_product
        self.clock = clock
        self.tag_timeout = tag_timeout
        self.rssi_window = rssi_window
        self.counted_idle = counted_idle
        self.on_booked_match = on_booked_match
        self._sessions: dict[str, _Session | _Counted] = {}
        #: Most recent successful link (for ``sensor.pantry_hub_last_link``).
        self.last_link: Link | None = None
        #: Station that last sent a barcode or tag (where "Tag them" goes).
        self.last_station: str | None = None
        #: Last counted session (kept after it ends, for "2 of 3 linked").
        self.counted: _Counted | None = None

    async def booked_match(self, link: Link) -> bool:
        """Use a unit booked without a tag for ``link``'s product, if any."""
        if not await self.store.take_booked_credit(
            link.product_id, self.clock(), BOOKED_MATCH_S
        ):
            return False
        _LOGGER.info(
            "Tag %s matched a %s unit already booked in Grocy: its arrival "
            "won't add stock again",
            link.epc,
            link.product_name,
        )
        if self.on_booked_match is not None:
            self.on_booked_match(link)
        return True

    def active(self, station: str) -> bool:
        """Whether ``station`` has a session waiting for a tag."""
        return station in self._sessions

    async def close(self) -> None:
        """Cancel every session."""
        sessions = list(self._sessions.values())
        self._sessions.clear()
        for session in sessions:
            if session.task is not None:
                session.task.cancel()
        for session in sessions:
            if session.task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await session.task

    async def _result(self, station: str, result: LinkResult) -> None:
        _LOGGER.info("Linking %s: %s (%s)", station, result.status, result.message)
        await self.publish(station, result)

    async def handle_barcode(self, station: str, payload: bytes) -> None:
        """Handle a barcode scanned in link mode."""
        if not STATION_ID_RE.fullmatch(station):
            _LOGGER.warning("Ignoring barcode from invalid station id %r", station)
            return
        data = _decode(payload)
        barcode = data.get("barcode") if data else None
        if isinstance(barcode, int) and not isinstance(barcode, bool):
            barcode = str(barcode)
        if (
            not isinstance(barcode, str)
            or not barcode.strip()
            or len(barcode) > BARCODE_MAX
        ):
            await self._result(
                station, LinkResult("error", "Bad barcode message from the station")
            )
            return
        barcode = barcode.strip()
        self.last_station = station
        old = self._sessions.pop(station, None)
        if old is not None and old.task is not None:
            old.task.cancel()
        session = _Session(
            station=station,
            barcode=barcode,
            started=self.clock(),
            in_stock=_in_stock(data),
        )
        self._sessions[station] = session
        session.task = asyncio.create_task(
            self._run(session), name=f"pantry_hub.link.{station}"
        )

    async def handle_tag(self, station: str, payload: bytes) -> None:
        """Handle a tag read in link mode."""
        if not STATION_ID_RE.fullmatch(station):
            _LOGGER.warning("Ignoring tag from invalid station id %r", station)
            return
        data = _decode(payload)
        epc = normalize_epc(data.get("epc")) if data else None
        if epc is None:
            await self._result(
                station, LinkResult("error", "Bad tag message from the station")
            )
            return
        self.last_station = station
        session = self._sessions.get(station)
        if session is None:
            await self._result(
                station, LinkResult("error", "Scan a barcode first", epc=epc)
            )
            return
        if isinstance(session, _Counted):
            self._collect(session, epc, data)
            return
        rssi = self._rssi(data)
        if _in_stock(data):
            session.in_stock = True
        now = self.clock()
        if session.window_opened is None:
            session.window_opened = now
            session.first_tag.set()
        elif now - session.window_opened > self.rssi_window:
            _LOGGER.debug("Ignoring tag %s after the RSSI window on %s", epc, station)
            return
        if epc not in session.tags:
            session.tags[epc] = rssi
        else:
            prev = session.tags[epc]
            if rssi is not None and (prev is None or rssi > prev):
                session.tags[epc] = rssi

    @staticmethod
    def _rssi(data: dict[str, Any] | None) -> float | None:
        raw = data.get("rssi") if data else None
        return (
            float(raw)
            if isinstance(raw, int | float) and not isinstance(raw, bool)
            else None
        )

    def _collect(self, s: _Counted, epc: str, data: dict[str, Any] | None) -> None:
        """Gather one tap's reads (strongest RSSI within the window wins)."""
        now = asyncio.get_running_loop().time()
        rssi = self._rssi(data)
        if s.window_opened is None:
            s.window_opened = now
            s.first_tag.set()
        elif now - s.window_opened > self.rssi_window:
            return
        prev = s.tags.get(epc)
        if epc not in s.tags or (rssi is not None and (prev is None or rssi > prev)):
            s.tags[epc] = rssi

    # Counted sessions ------------------------------------------------------------

    async def start_counted(
        self,
        station: str,
        product: Product,
        barcode: str,
        count: int,
        *,
        on_each: Callable[[Link], Awaitable[None]] | None = None,
    ) -> None:
        """Link up to ``count`` tags to ``product`` at ``station``.

        Replaces any session on that station. The caller tells the station to
        enter link mode (``linking/<station>/start``).
        """
        if not STATION_ID_RE.fullmatch(station):
            raise ValueError(f"invalid station id {station!r}")
        if count < 1:
            raise ValueError("count must be at least 1")
        old = self._sessions.pop(station, None)
        if old is not None and old.task is not None:
            old.task.cancel()
        s = _Counted(
            station=station,
            barcode=barcode,
            product=product,
            count=count,
            last_tap=asyncio.get_running_loop().time(),
            on_each=on_each,
        )
        self._sessions[station] = s
        self.counted = s
        s.task = asyncio.create_task(
            self._run_counted(s), name=f"pantry_hub.link_counted.{station}"
        )

    def _counted_result(
        self, s: _Counted, status: Status, message: str, epc: str = ""
    ) -> LinkResult:
        return LinkResult(
            status,
            message,
            product=s.product.name,
            barcode=s.barcode,
            epc=epc,
            count=s.count,
            linked=s.linked,
        )

    async def _run_counted(self, s: _Counted) -> None:
        loop = asyncio.get_running_loop()
        try:
            while s.linked < s.count:
                remaining = self.counted_idle - (loop.time() - s.last_tap)
                try:
                    async with asyncio.timeout(max(remaining, 0.0)):
                        await s.first_tag.wait()
                except TimeoutError:
                    s.ended = "timeout"
                    await self._result(
                        s.station,
                        self._counted_result(
                            s,
                            "timeout",
                            f"No tag for {self.counted_idle:g} s: "
                            f"{s.linked} of {s.count} linked",
                        ),
                    )
                    return
                await asyncio.sleep(self.rssi_window)
                tags, s.tags = s.tags, {}
                s.first_tag.clear()
                s.window_opened = None
                s.last_tap = loop.time()
                await self._counted_tap(s, self.choose(tags))
            s.ended = "done"
        except asyncio.CancelledError:
            s.ended = s.ended or "cancelled"
            raise
        except Exception as err:
            s.ended = "error"
            _LOGGER.exception("Counted linking failed on %s", s.station)
            await self._result(
                s.station, self._counted_result(s, "error", f"Internal error: {err}")
            )
        finally:
            if self._sessions.get(s.station) is s:
                del self._sessions[s.station]

    async def _counted_tap(self, s: _Counted, epc: str) -> None:
        existing = await self.store.get_link(epc)
        if existing is not None:
            s.skipped += 1
            same = existing.product_id == s.product.id
            text = (
                f"Already linked to {existing.product_name}; skipped"
                if same
                else f"Tag is linked to {existing.product_name}; skipped"
            )
            await self._result(
                s.station,
                self._counted_result(
                    s,
                    "tag_already_linked",
                    f"{text} ({s.linked} of {s.count} linked)",
                    epc,
                ),
            )
            return
        link = Link(
            epc=epc,
            product_id=s.product.id,
            product_name=s.product.name,
            linked_at=self.clock(),
            barcode=s.barcode,
            default_best_before_days=s.product.default_best_before_days,
            station=s.station,
        )
        in_stock = await self.booked_match(link)
        created, op_id = await self.store.create_link(
            link,
            location_id=self.location_id,
            best_before=best_before_for(link, self.clock()),
            in_stock=in_stock,
        )
        if not created:  # pragma: no cover - linked between the check and here
            s.skipped += 1
            return
        s.linked += 1
        self.last_link = link
        await self._result(
            s.station,
            self._counted_result(
                s,
                "linked",
                f"Linked {s.linked} of {s.count}: {s.product.name}",
                epc,
            ),
        )
        if self.on_linked is not None:
            await self.on_linked(link, op_id)
        if s.on_each is not None:
            await s.on_each(link)

    # Barcode sessions --------------------------------------------------------------

    async def _run(self, session: _Session) -> None:
        station, barcode = session.station, session.barcode
        try:
            product = await self._lookup(station, barcode)
            if product is None:
                return
            remaining = self.tag_timeout - (self.clock() - session.started)
            try:
                async with asyncio.timeout(max(remaining, 0.0)):
                    await session.first_tag.wait()
            except TimeoutError:
                await self._result(
                    station,
                    LinkResult(
                        "timeout",
                        f"No tag read within {self.tag_timeout:.0f} s",
                        product=product.name,
                        barcode=barcode,
                    ),
                )
                return
            await asyncio.sleep(self.rssi_window)
            await self._finish(session, product)
        except asyncio.CancelledError:
            raise
        except Exception as err:
            _LOGGER.exception("Linking failed on %s for barcode %s", station, barcode)
            await self._result(
                station, LinkResult("error", f"Internal error: {err}", barcode=barcode)
            )
        finally:
            if self._sessions.get(station) is session:
                del self._sessions[station]

    async def _lookup(self, station: str, barcode: str) -> Product | None:
        try:
            product = await self.backend.product_by_barcode(barcode)
            if product is None:
                product = await self.backend.external_lookup(barcode)
                if product is not None:
                    _LOGGER.info(
                        "Created Grocy product %r from the barcode lookup plugin",
                        product.name,
                    )
        except GrocyAuthError as err:
            await self._result(station, LinkResult("error", str(err), barcode=barcode))
            return None
        except GrocyError as err:
            await self._result(
                station,
                LinkResult("error", f"Grocy unavailable: {err}", barcode=barcode),
            )
            return None
        if product is None:
            await self.store.add_needs_product(barcode, station, self.clock())
            if self.on_needs_product is not None:
                self.on_needs_product()
            await self._result(
                station,
                LinkResult(
                    "unknown_barcode",
                    "Grocy has no product with this barcode; added to the "
                    "'needs product' list",
                    barcode=barcode,
                ),
            )
        return product

    @staticmethod
    def choose(tags: dict[str, float | None]) -> str:
        """Strongest RSSI wins; unknown RSSI loses; ties keep the first seen."""
        best_epc = ""
        best: float | None = None
        for epc, rssi in tags.items():
            if not best_epc or (rssi is not None and (best is None or rssi > best)):
                best_epc, best = epc, rssi
        return best_epc

    async def _finish(self, session: _Session, product: Product) -> None:
        station, barcode = session.station, session.barcode
        epc = self.choose(session.tags)
        existing = await self.store.get_link(epc)
        if existing is not None:
            await self._result(
                station,
                LinkResult(
                    "tag_already_linked",
                    f"Tag is already linked to {existing.product_name}; "
                    "relink it in the Pantry Hub web UI",
                    product=existing.product_name,
                    barcode=barcode,
                    epc=epc,
                ),
            )
            return
        link = Link(
            epc=epc,
            product_id=product.id,
            product_name=product.name,
            linked_at=self.clock(),
            barcode=barcode,
            default_best_before_days=product.default_best_before_days,
            station=station,
        )
        in_stock = session.in_stock or await self.booked_match(link)
        created, op_id = await self.store.create_link(
            link,
            location_id=self.location_id,
            best_before=best_before_for(link, self.clock()),
            in_stock=in_stock,
        )
        if not created:
            again = await self.store.get_link(epc)
            name = again.product_name if again else ""
            await self._result(
                station,
                LinkResult(
                    "tag_already_linked",
                    f"Tag is already linked to {name}",
                    product=name,
                    barcode=barcode,
                    epc=epc,
                ),
            )
            return
        self.last_link = link
        await self._result(
            station,
            LinkResult(
                "linked",
                f"Linked to {product.name}"
                + (" (already in stock: no add for this arrival)" if in_stock else ""),
                product=product.name,
                barcode=barcode,
                epc=epc,
            ),
        )
        if self.on_linked is not None:
            await self.on_linked(link, op_id)


def best_before_for(link: Link, now: float) -> str | None:
    """Best-before date to send with an add, or None to leave it to Grocy.

    Uses the link's own date if set, else the product's default best-before
    days (Grocy's ``-1`` means "never expires": 2999-12-31, as Grocy's UI does).
    """
    if link.best_before:
        return link.best_before
    days = link.default_best_before_days
    if days is None or days == 0:
        return None
    if days < 0:
        return "2999-12-31"
    return (datetime.fromtimestamp(now, UTC) + timedelta(days=days)).date().isoformat()
