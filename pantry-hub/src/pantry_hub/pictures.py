"""Product pictures from Kroger Restock, stored in Grocy.

On a barcode link (and from the "Fetch missing pictures" backfill), for a
Grocy product without a picture::

    kroger_restock.lookup_product(upc=<barcode>)  (Home Assistant action; only
                                                  the barcode leaves the house)
    → download image_url (10 s, 5 MB, image/* only, http/https only)
    → resize to 600 px long edge, JPEG q85
    → PUT /api/files/productpictures/<base64 "pantryhub_<id>.jpg">
    → PUT /api/objects/products/<id> {"picture_file_name": ...}

Only products whose ``picture_file_name`` is empty or starts with
``pantryhub_`` are ever touched, so pictures set by a person stay.
Nothing here blocks linking: pictures are fetched in the background, retried
with backoff while Home Assistant / Grocy / the image host fail. Logs never
contain image URLs (they may carry signed tokens).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
import contextlib
from dataclasses import dataclass
import io
import logging
import time
from typing import Any, Literal
from urllib.parse import urlsplit

import aiohttp
from PIL import Image, UnidentifiedImageError

from .grocy import GrocyClient, GrocyError
from .ha_api import HomeAssistantApi, HomeAssistantApiError

_LOGGER = logging.getLogger(__name__)

DOMAIN = "kroger_restock"
LOOKUP_SERVICE = "lookup_product"
PREFIX = "pantryhub_"
MAX_BYTES = 5 * 1024 * 1024
DOWNLOAD_TIMEOUT_S = 10.0
LONG_EDGE = 600
JPEG_QUALITY = 85
#: A "Kroger has no picture" answer is remembered this long.
MISS_TTL_S = 30 * 86400.0
#: Backfill: seconds between two lookups.
BACKFILL_SPACING_S = 2.0
#: Background retries after a failed attempt (seconds before each retry).
RETRY_DELAYS_S: tuple[float, ...] = (10.0, 60.0, 300.0)
NOT_FOUND_LINE = "Product pictures: Kroger Restock not found."
SIGN_IN_TEXT = "Kroger Restock needs you to sign in again"
#: ``reason`` values of a ``found: false`` answer that mean "no such product
#: at Kroger": remembered for :data:`MISS_TTL_S`.
MISS_REASONS = frozenset({"not_found", "plu_code", "not_a_product_barcode"})
#: After ``reason: rate_limited`` no lookup is made for this long.
RATE_LIMIT_BACKOFF_S = 3600.0

Outcome = Literal[
    "set",
    "has_picture",
    "not_found",
    "skipped",
    "no_kroger",
    "rate_limited",
    "auth_failed",
]


class PictureError(Exception):
    """A step failed in a way worth retrying later (network, Grocy, HA)."""


class BadImage(Exception):
    """The image itself is unusable (too big, not an image); don't retry."""


def picture_filename(product_id: int) -> str:
    """Grocy picture file name Pantry Hub uses for a product."""
    return f"{PREFIX}{product_id}.jpg"


def ours_or_empty(name: object) -> bool:
    """Whether Pantry Hub may set this product's picture."""
    return not name or (isinstance(name, str) and name.startswith(PREFIX))


def resize_jpeg(data: bytes) -> bytes:
    """Any image → JPEG q85, long edge at most 600 px (never upscaled)."""
    try:
        with Image.open(io.BytesIO(data)) as src:
            src.load()
            src.thumbnail((LONG_EDGE, LONG_EDGE))
            img: Image.Image = src
            if img.mode not in ("RGB", "L"):
                rgba = img.convert("RGBA")
                img = Image.new("RGB", rgba.size, (255, 255, 255))
                img.paste(rgba, mask=rgba.getchannel("A"))
            out = io.BytesIO()
            img.save(out, "JPEG", quality=JPEG_QUALITY, optimize=True)
            return out.getvalue()
    except (UnidentifiedImageError, OSError, ValueError) as err:
        raise BadImage(f"not a readable image: {err}") from err


@dataclass(slots=True)
class BackfillProgress:
    """What the Settings page shows while (and after) a backfill runs."""

    running: bool = False
    done: int = 0
    total: int = 0
    not_found: int = 0
    set: int = 0
    failed: int = 0
    finished_at: float | None = None
    #: Why it stopped early (signed out / rate limited), if it did.
    stopped: str | None = None

    def text(self) -> str:
        """``12 of 40, 3 not found``."""
        if self.total == 0 and not self.running:
            return "Every product already has a picture"
        parts = [f"{self.done} of {self.total}"]
        if self.not_found:
            parts.append(f"{self.not_found} not found")
        if self.failed:
            parts.append(f"{self.failed} failed")
        text = ", ".join(parts)
        return f"{text}. Stopped: {self.stopped}" if self.stopped else text


class PictureService:
    """Fetches product pictures through Kroger Restock into Grocy."""

    def __init__(
        self,
        grocy: GrocyClient,
        ha: HomeAssistantApi | None,
        store: Any,
        *,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        retry_delays: Sequence[float] = RETRY_DELAYS_S,
        spacing_s: float = BACKFILL_SPACING_S,
        on_change: Callable[[], None] | None = None,
    ) -> None:
        """Create. ``ha`` None: Home Assistant API unavailable (skip silently)."""
        self.grocy = grocy
        self.ha = ha
        self.store = store
        self.clock = clock
        self.sleep = sleep
        self.retry_delays = tuple(retry_delays)
        self.spacing_s = spacing_s
        self.on_change = on_change
        #: Last answer to "is kroger_restock.lookup_product there?" (None: not asked).
        self.available: bool | None = None
        self.progress = BackfillProgress()
        #: Kroger Restock answered ``auth_failed``: no lookups until a person
        #: presses "Fetch missing pictures" (or the app restarts).
        self.auth_failed = False
        #: Kroger Restock answered ``rate_limited``: no lookups before this.
        self.paused_until: float | None = None
        #: Bumped when Pantry Hub sets a product's picture (busts caches).
        self.versions: dict[int, int] = {}
        self._tasks: dict[int, asyncio.Task[None]] = {}
        self._backfill: asyncio.Task[None] | None = None
        self._http: aiohttp.ClientSession | None = None

    # Lifecycle -----------------------------------------------------------------

    async def close(self) -> None:
        """Cancel background work and close the download session."""
        tasks = [*self._tasks.values()]
        if self._backfill is not None:
            tasks.append(self._backfill)
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
        self._backfill = None
        if self._http is not None:
            await self._http.close()
            self._http = None

    def status_line(self) -> str | None:
        """Details value when lookups can't run (missing, signed out, limited)."""
        if self.available is False:
            return "Kroger Restock not found."
        if self.auth_failed:
            return SIGN_IN_TEXT
        if self.paused_until is not None and self.clock() < self.paused_until:
            return "Kroger rate limit: paused for up to an hour"
        return None

    def _changed(self) -> None:
        if self.on_change is not None:
            self.on_change()

    # On link -------------------------------------------------------------------

    def on_link(self, product_id: int, barcode: str | None) -> None:
        """Fetch a picture in the background after a barcode link."""
        if not barcode or self.ha is None or product_id in self._tasks:
            return
        task = asyncio.create_task(
            self._with_retry(product_id, barcode),
            name=f"pantry_hub.picture.{product_id}",
        )
        self._tasks[product_id] = task
        task.add_done_callback(lambda _t: self._tasks.pop(product_id, None))

    async def wait_idle(self) -> None:
        """Wait for background picture work (tests)."""
        while self._tasks or (self._backfill is not None and not self._backfill.done()):
            pending = [*self._tasks.values()]
            if self._backfill is not None:
                pending.append(self._backfill)
            await asyncio.gather(*pending, return_exceptions=True)

    async def _with_retry(self, product_id: int, barcode: str) -> None:
        delays = (0.0, *self.retry_delays)
        for attempt, delay in enumerate(delays, start=1):
            if delay:
                await self.sleep(delay)
            try:
                outcome = await self.fetch(product_id, barcode)
                if outcome == "rate_limited" and self.paused_until is not None:
                    # Try once more when the back-off is over.
                    await self.sleep(max(self.paused_until - self.clock(), 0.0))
                    await self.fetch(product_id, barcode)
                return
            except PictureError as err:
                level = logging.WARNING if attempt == len(delays) else logging.INFO
                _LOGGER.log(
                    level,
                    "Picture for Grocy product %d: %s (attempt %d of %d)",
                    product_id,
                    err,
                    attempt,
                    len(delays),
                )
            except BadImage as err:
                _LOGGER.warning("Picture for Grocy product %d: %s", product_id, err)
                return

    # One product ---------------------------------------------------------------

    async def refresh_available(self) -> bool:
        """Ask Home Assistant whether Kroger Restock's lookup exists."""
        return await self._kroger_available()

    async def _kroger_available(self) -> bool:
        if self.ha is None:
            self.available = False
            return False
        found = await self.ha.has_service(DOMAIN, LOOKUP_SERVICE)
        if found != self.available:
            self.available = found
            self._changed()
        return found

    def blocked(self) -> Outcome | None:
        """Why no lookup may be made now (signed out / rate limited), if so."""
        if self.auth_failed:
            return "auth_failed"
        if self.paused_until is not None:
            if self.clock() < self.paused_until:
                return "rate_limited"
            self.paused_until = None
            self._changed()
        return None

    async def lookup(self, barcode: str) -> dict[str, Any] | None:
        """``kroger_restock.lookup_product`` response, or None if it's missing.

        Kroger Restock v0.17.2 takes ``upc`` *or* ``product_id`` (plus an
        optional ``config_entry_id``) and normalizes UPC-A, EAN-13, UPC-E and
        padding itself, so the code is passed as scanned / stored in Grocy.
        A ``found: false`` answer is returned as it is; ``rate_limited`` and
        ``auth_failed`` also pause later lookups.
        """
        if not await self._kroger_available():
            return None
        assert self.ha is not None
        try:
            info = await self.ha.call(DOMAIN, LOOKUP_SERVICE, {"upc": barcode})
        except HomeAssistantApiError as err:
            raise PictureError(f"product lookup failed: {err}") from err
        if not info.get("found"):
            self._note_reason(info.get("reason"))
        return info

    def _note_reason(self, reason: object) -> None:
        """Act on a ``found: false`` reason that affects every lookup."""
        if reason == "rate_limited":
            self.paused_until = self.clock() + RATE_LIMIT_BACKOFF_S
            _LOGGER.warning(
                "Kroger rate limit: no product lookups for %d min",
                RATE_LIMIT_BACKOFF_S // 60,
            )
            self._changed()
        elif reason == "auth_failed" and not self.auth_failed:
            self.auth_failed = True
            _LOGGER.warning("%s; product pictures paused", SIGN_IN_TEXT)
            self._changed()

    async def fetch(
        self, product_id: int, barcode: str, *, replace_ours: bool = False
    ) -> Outcome:
        """Set the product's picture (and empty name/description) from Kroger.

        ``replace_ours``: also replace a ``pantryhub_`` picture (backfill, when
        Grocy no longer has its file).

        Raises :class:`PictureError` for failures worth retrying and
        :class:`BadImage` for an unusable image.
        """
        try:
            row = await self.grocy.product_object(product_id)
        except GrocyError as err:
            raise PictureError(f"Grocy: {err}") from err
        current = row.get("picture_file_name")
        if not ours_or_empty(current):
            return "skipped"  # a person chose this picture
        if current and not replace_ours:
            return "has_picture"
        blocked = self.blocked()
        if blocked is not None:
            return blocked
        info = await self.lookup(barcode)
        if info is None:
            return "no_kroger"
        if not info.get("found"):
            reason = info.get("reason")
            if reason == "rate_limited":
                return "rate_limited"
            if reason == "auth_failed":
                return "auth_failed"
            if reason not in MISS_REASONS:
                raise PictureError(f"Kroger Restock lookup failed ({reason})")
            await self.store.set_picture_miss(product_id, self.clock())
            return "not_found"
        await self._fill_text(product_id, row, info, barcode)
        url = info.get("image_url")
        if not isinstance(url, str) or not url:
            await self.store.set_picture_miss(product_id, self.clock())
            return "not_found"
        image = await self.download(url)
        jpeg = await asyncio.to_thread(resize_jpeg, image)
        name = picture_filename(product_id)
        try:
            await self.grocy.upload_product_picture(name, jpeg)
            await self.grocy.update_product(product_id, {"picture_file_name": name})
        except GrocyError as err:
            raise PictureError(f"Grocy upload: {err}") from err
        await self.store.set_picture_miss(product_id, None)
        self.versions[product_id] = self.versions.get(product_id, 0) + 1
        _LOGGER.info("Set the picture of Grocy product %d from Kroger", product_id)
        self._changed()
        return "set"

    async def _fill_text(
        self, product_id: int, row: dict[str, Any], info: dict[str, Any], barcode: str
    ) -> None:
        """Fill an empty name / description; never overwrite a person's text."""
        if not info.get("found"):
            return
        fields: dict[str, Any] = {}
        name = str(row.get("name") or "").strip()
        new_name = info.get("name")
        # Empty, or the placeholder some lookups leave: the barcode itself.
        placeholders = {barcode, info.get("barcode"), info.get("upc")}
        if (
            (not name or name in placeholders)
            and isinstance(new_name, str)
            and new_name
        ):
            fields["name"] = new_name.strip()[:200]
        if not str(row.get("description") or "").strip():
            desc = " · ".join(
                str(v).strip()
                for v in (info.get("brand"), info.get("size"))
                if isinstance(v, str) and v.strip()
            )
            if desc:
                fields["description"] = desc
        if fields:
            try:
                await self.grocy.update_product(product_id, fields)
            except GrocyError as err:
                raise PictureError(f"Grocy update: {err}") from err

    async def download(self, url: str) -> bytes:
        """Fetch an image within the limits (see module docstring)."""
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise BadImage("image URL is not http(s)")
        host = parts.hostname
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession()
        try:
            async with self._http.get(
                url,
                timeout=aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT_S),
                allow_redirects=True,
                max_redirects=3,
            ) as resp:
                if resp.status >= 500 or resp.status == 429:
                    raise PictureError(f"image host {host} answered {resp.status}")
                if resp.status >= 400:
                    raise BadImage(f"image host {host} answered {resp.status}")
                ctype = resp.headers.get("Content-Type", "").split(";")[0].strip()
                if not ctype.lower().startswith("image/"):
                    raise BadImage(f"image host {host} sent {ctype or 'no type'}")
                if (resp.content_length or 0) > MAX_BYTES:
                    raise BadImage(f"image from {host} is larger than 5 MB")
                data = bytearray()
                async for chunk in resp.content.iter_chunked(65536):
                    data += chunk
                    if len(data) > MAX_BYTES:
                        raise BadImage(f"image from {host} is larger than 5 MB")
                return bytes(data)
        except (aiohttp.ClientError, TimeoutError) as err:
            # str(err) of a client error can contain the URL: name the host only.
            raise PictureError(
                f"downloading from {host} failed ({type(err).__name__})"
            ) from None

    # Backfill ------------------------------------------------------------------

    def start_backfill(self, products: dict[int, str | None]) -> bool:
        """Start "Fetch missing pictures"; False if one is already running."""
        if self._backfill is not None and not self._backfill.done():
            return False
        # A person pressing it may have signed in to Kroger Restock again:
        # lift an auth_failed pause (not a rate limit: Kroger decides that).
        if self.auth_failed:
            self.auth_failed = False
            _LOGGER.info("Trying Kroger Restock again after a sign-in failure")
        self.progress = BackfillProgress(running=True)
        self._changed()
        self._backfill = asyncio.create_task(
            self._run_backfill(products), name="pantry_hub.picture_backfill"
        )
        return True

    async def _needs_picture(self, product_id: int) -> tuple[bool, bool]:
        """``(needs a picture, replaces one of ours whose file is gone)``."""
        row = await self.grocy.product_object(product_id)
        name = row.get("picture_file_name")
        if not name:
            return True, False
        if isinstance(name, str) and name.startswith(PREFIX):
            # Ours, but the file may have been deleted in Grocy.
            gone = not await self.grocy.picture_exists(name)
            return gone, gone
        return False, False

    async def _run_backfill(self, products: dict[int, str | None]) -> None:
        progress = self.progress
        try:
            now = self.clock()
            todo: list[tuple[int, str]] = []
            ours: set[int] = set()
            for pid, known in products.items():
                missed = await self.store.picture_miss(pid)
                if missed is not None and now - missed < MISS_TTL_S:
                    continue
                try:
                    needed, replace = await self._needs_picture(pid)
                    if not needed:
                        continue
                    barcode = known or await self.grocy.first_barcode(pid)
                except GrocyError as err:
                    _LOGGER.warning("Picture backfill: Grocy product %d: %s", pid, err)
                    continue
                if barcode:
                    todo.append((pid, barcode))
                    if replace:
                        ours.add(pid)
            progress.total = len(todo)
            self._changed()
            if todo and not await self._kroger_available():
                progress.total = 0
                return
            for i, (pid, barcode) in enumerate(todo):
                if i:
                    await self.sleep(self.spacing_s)
                try:
                    outcome = await self.fetch(pid, barcode, replace_ours=pid in ours)
                    if outcome in ("rate_limited", "auth_failed"):
                        progress.stopped = (
                            SIGN_IN_TEXT
                            if outcome == "auth_failed"
                            else "Kroger rate limit, try again in an hour"
                        )
                        break
                    if outcome == "not_found":
                        progress.not_found += 1
                    else:
                        progress.set += 1
                except (PictureError, BadImage) as err:
                    progress.failed += 1
                    _LOGGER.warning("Picture backfill: Grocy product %d: %s", pid, err)
                progress.done += 1
                self._changed()
        finally:
            progress.running = False
            progress.finished_at = self.clock()
            _LOGGER.info("Picture backfill finished: %s", progress.text())
            self._changed()
