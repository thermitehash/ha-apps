"""Linking workflow: barcode + tag from a linking station → EPC ↔ product link.

MQTT topics (prefix ``pantry_hub/``)::

    linking/<station_id>/barcode  ← {"barcode": "...", "ts": ...}
    linking/<station_id>/tag      ← {"epc": "...", "rssi": -45.2, "ts": ...}
    linking/<station_id>/result   → {"status": "...", "product": "...", "message": "..."}

``status`` is one of ``linked``, ``unknown_barcode``, ``tag_already_linked``,
``timeout`` or ``error``.

Linking never changes Grocy stock: if the item then goes into the pantry, the
normal arrival flow adds it (no double counting).
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
        return out


@dataclass(slots=True)
class _Session:
    station: str
    barcode: str
    started: float
    tags: dict[str, float | None] = field(default_factory=dict)
    first_tag: asyncio.Event = field(default_factory=asyncio.Event)
    window_opened: float | None = None
    task: asyncio.Task[None] | None = None


def normalize_epc(raw: object) -> str | None:
    """Upper-case hex EPC (whole 16-bit words), or None if invalid."""
    if not isinstance(raw, str):
        return None
    epc = raw.replace(" ", "").replace(":", "").upper()
    return epc if EPC_RE.fullmatch(epc) else None


def _decode(payload: bytes) -> dict[str, Any] | None:
    try:
        data = json.loads(payload)
    except UnicodeDecodeError, ValueError:
        return None
    return data if isinstance(data, dict) else None


PublishFn = Callable[[str, LinkResult], Awaitable[None]]
LinkedFn = Callable[[Link, int | None], Awaitable[None]]


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
        self._sessions: dict[str, _Session] = {}
        #: Most recent successful link (for ``sensor.pantry_hub_last_link``).
        self.last_link: Link | None = None

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
        old = self._sessions.pop(station, None)
        if old is not None and old.task is not None:
            old.task.cancel()
        session = _Session(station=station, barcode=barcode, started=self.clock())
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
        session = self._sessions.get(station)
        if session is None:
            await self._result(
                station, LinkResult("error", "Scan a barcode first", epc=epc)
            )
            return
        raw_rssi = data.get("rssi") if data else None
        rssi = (
            float(raw_rssi)
            if isinstance(raw_rssi, int | float) and not isinstance(raw_rssi, bool)
            else None
        )
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
        created, op_id = await self.store.create_link(
            link,
            location_id=self.location_id,
            best_before=best_before_for(link, self.clock()),
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
                f"Linked to {product.name}",
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
