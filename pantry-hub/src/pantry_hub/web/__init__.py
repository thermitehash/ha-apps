"""Ingress web UI: one HTML shell + vanilla JS modules, data from ``/api/state``.

Home Assistant ingress authenticates the user; this server only accepts
connections from the Supervisor's ingress proxy (172.30.32.2) unless told
otherwise for local development. Every action is a POST carrying a
per-process CSRF token (form field ``csrf`` or header ``X-CSRF-Token``) and
answers JSON ``{"ok": bool, "message": str}``; the page then re-renders from
``/api/state``.

No build step: ``static/`` holds plain ES modules and CSS. JS modules import
each other as ``./x.js?v=__VERSION__``; the placeholder is replaced when
served, so a new app version never mixes with cached old modules.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
import html
import json
import logging
from pathlib import Path
import secrets
import time
from typing import TYPE_CHECKING

import aiohttp
from aiohttp import web

from ..grocy import GrocyError
from ..linking import normalize_epc
from .state import build_state

if TYPE_CHECKING:
    from ..hub import Hub

_LOGGER = logging.getLogger(__name__)

INGRESS_PROXY = "172.30.32.2"
ALLOW_KEY = web.AppKey("allow", frozenset)
STATIC_DIR = Path(__file__).with_name("static")
#: Long cache for versioned static assets (``?v=<version>`` busts it).
STATIC_CACHE = "public, max-age=31536000, immutable"
STATIC_TYPES = {
    ".js": "text/javascript",
    ".css": "text/css",
    ".svg": "image/svg+xml",
}
#: Product pictures are fetched from Grocy at most this often per product.
PICTURE_TTL_S = 3600.0
ActionFn = Callable[[dict[str, str]], Awaitable[str]]


@web.middleware
async def no_store(
    request: web.Request,
    handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
) -> web.StreamResponse:
    """Never let a web view show a cached page or state.

    The HA companion app's web view could show an old copy of the page after
    opening Pantry Hub from the side menu. HTML and JSON are ``no-store``;
    static assets are versioned and cached for a year.
    """
    try:
        response = await handler(request)
    except web.HTTPException as exc:
        if not request.path.startswith("/static/"):
            exc.headers["Cache-Control"] = "no-store"
        raise
    if request.path.startswith("/static/") and response.status == 200:
        response.headers["Cache-Control"] = STATIC_CACHE
    elif request.path.startswith("/api/product-picture/") and response.status == 200:
        response.headers["Cache-Control"] = "private, max-age=3600"
    else:
        response.headers["Cache-Control"] = "no-store"
        response.headers.setdefault("Pragma", "no-cache")
    return response


class WebUI:
    """aiohttp app bound to a :class:`Hub`."""

    def __init__(self, hub: Hub, *, allowed_peers: Iterable[str] | None = None) -> None:
        """Create; :meth:`start` binds the port."""
        self.hub = hub
        self.token = secrets.token_urlsafe(24)
        allow = (
            frozenset(allowed_peers)
            if allowed_peers is not None
            else frozenset({INGRESS_PROXY})
        )
        self.app = web.Application(middlewares=[self._guard, no_store])
        self.app[ALLOW_KEY] = allow
        r = self.app.router
        r.add_get("/", self.index)
        r.add_get("/static/{name}", self.static)
        r.add_get("/api/state", self.api_state)
        r.add_get("/api/door", self.api_door)
        r.add_get("/api/door-candidates", self.api_door_candidates)
        r.add_get("/api/product-picture/{product_id}", self.api_product_picture)
        actions: dict[str, ActionFn] = {
            "/unlink": self._unlink,
            "/relink": self._relink,
            "/forget": self._forget,
            "/dismiss": self._dismiss,
            "/retry": self._retry,
            "/consume-now": self._consume_now,
            "/dismiss-op": self._dismiss_op,
            "/resync": self._resync,
            "/sweep": self._sweep,
            "/settings/door": self._set_door,
            "/settings/inverted": self._set_inverted,
        }
        for path, action in actions.items():
            r.add_post(path, self._action(action))
        self._runner: web.AppRunner | None = None
        self._pictures: dict[int, tuple[float, tuple[bytes, str] | None]] = {}

    async def start(self, host: str, port: int) -> None:
        """Serve on ``host:port``."""
        self._runner = web.AppRunner(self.app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, host, port)
        await site.start()
        _LOGGER.info("Web UI listening on %s:%d (ingress)", host, port)

    async def stop(self) -> None:
        """Stop serving."""
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    @web.middleware
    async def _guard(
        self,
        request: web.Request,
        handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
    ) -> web.StreamResponse:
        allow: frozenset[str] = request.app[ALLOW_KEY]
        if "*" not in allow and request.remote not in allow:
            _LOGGER.warning("Refused web request from %s (not ingress)", request.remote)
            raise web.HTTPForbidden(
                text="Use Pantry Hub through Home Assistant ingress"
            )
        return await handler(request)

    # Page and assets -------------------------------------------------------------

    @staticmethod
    def base(request: web.Request) -> str:
        """Return the ingress prefix (``X-Ingress-Path``) every URL is built from."""
        return request.headers.get("X-Ingress-Path", "").rstrip("/")

    async def index(self, request: web.Request) -> web.Response:
        """Serve the HTML shell; the page fills itself from ``/api/state``."""
        base = self.base(request)
        v = self.hub.version
        boot = {"base": base, "csrf": self.token, "version": v}
        page = SHELL.format(
            base=html.escape(base, quote=True),
            v=html.escape(v, quote=True),
            boot=html.escape(json.dumps(boot), quote=True),
        )
        return web.Response(text=page, content_type="text/html")

    async def static(self, request: web.Request) -> web.Response:
        """Serve a file from ``static/`` (no directory listing, no traversal)."""
        name = request.match_info["name"]
        path = STATIC_DIR / name
        suffix = path.suffix
        if (
            "/" in name
            or name.startswith(".")
            or suffix not in STATIC_TYPES
            or not path.is_file()
        ):
            raise web.HTTPNotFound()
        text = path.read_text(encoding="utf-8")
        if suffix == ".js":
            text = text.replace("__VERSION__", self.hub.version)
        return web.Response(text=text, content_type=STATIC_TYPES[suffix])

    # JSON ------------------------------------------------------------------------

    async def api_state(self, request: web.Request) -> web.Response:
        """Everything the page shows (polled every 3 s while visible)."""
        return web.json_response(await build_state(self.hub))

    async def api_door(self, request: web.Request) -> web.Response:
        """Live door status: interpreted and raw (polled every second)."""
        return web.json_response(self.hub.door_status())

    async def api_door_candidates(self, request: web.Request) -> web.Response:
        """binary_sensors for the picker, door-like first."""
        try:
            candidates = await self.hub.door_candidates()
        except (ValueError, OSError, TimeoutError, aiohttp.ClientError) as err:
            return web.json_response({"candidates": [], "error": str(err)})
        return web.json_response(
            {
                "candidates": [
                    {
                        "entity_id": c.entity_id,
                        "name": c.name,
                        "state": c.state,
                        "device_class": c.device_class,
                        "door_like": c.door_like,
                    }
                    for c in candidates
                ],
                "error": None,
            }
        )

    async def api_product_picture(self, request: web.Request) -> web.Response:
        """Proxy the Grocy product picture (thumbnail); 404 if there is none."""
        try:
            product_id = int(request.match_info["product_id"])
        except ValueError as err:
            raise web.HTTPNotFound() from err
        picture = await self._picture(product_id)
        if picture is None:
            # 204, not 404: the <img> falls back to an icon without a console error.
            return web.Response(status=204)
        body, ctype = picture
        return web.Response(body=body, content_type=ctype)

    async def _picture(self, product_id: int) -> tuple[bytes, str] | None:
        cached = self._pictures.get(product_id)
        now = time.monotonic()
        if cached is not None and now - cached[0] < PICTURE_TTL_S:
            return cached[1]
        fetch = getattr(self.hub.backend, "product_picture", None)
        picture: tuple[bytes, str] | None = None
        if fetch is not None:
            try:
                picture = await fetch(product_id)
            except GrocyError as err:
                _LOGGER.debug("No picture for product %d: %s", product_id, err)
        self._pictures[product_id] = (now, picture)
        return picture

    # Actions ---------------------------------------------------------------------

    def _action(
        self, action: ActionFn
    ) -> Callable[[web.Request], Awaitable[web.Response]]:
        async def handler(request: web.Request) -> web.Response:
            form: dict[str, str]
            if request.content_type == "application/json":
                try:
                    body = await request.json()
                except ValueError as err:
                    raise web.HTTPBadRequest(text="Body is not JSON") from err
                if not isinstance(body, dict):
                    raise web.HTTPBadRequest(text="Body must be a JSON object")
                form = {k: "" if v is None else str(v) for k, v in body.items()}
            else:
                data = await request.post()
                form = {k: str(v) for k, v in data.items()}
            token = request.headers.get("X-CSRF-Token") or form.get("csrf", "")
            if not secrets.compare_digest(token, self.token):
                return web.json_response(
                    {"ok": False, "message": "Stale page; reload it"}, status=403
                )
            try:
                message = await action(form)
            except (GrocyError, ValueError) as err:
                return web.json_response({"ok": False, "message": f"Failed: {err}"})
            return web.json_response({"ok": True, "message": message})

        return handler

    async def _unlink(self, form: dict[str, str]) -> str:
        epc = form.get("epc", "")
        ok = await self.hub.unlink(epc)
        return f"Unlinked {epc}" if ok else f"{epc} was not linked"

    async def _relink(self, form: dict[str, str]) -> str:
        epc = normalize_epc(form.get("epc", ""))
        if epc is None:
            raise ValueError("not a valid EPC")
        target = form.get("target", "").strip()
        if not target:
            raise ValueError("enter a barcode or a Grocy product id (#12)")
        in_stock = form.get("in_stock") in ("on", "true", "1")
        if target.startswith("#") and target[1:].isdigit():
            link = await self.hub.relink(
                epc, product_id=int(target[1:]), in_stock=in_stock
            )
        else:
            link = await self.hub.relink(epc, barcode=target, in_stock=in_stock)
        suffix = " (already in stock: no add for this arrival)" if in_stock else ""
        return f"Linked {epc} to {link.product_name}{suffix}"

    async def _forget(self, form: dict[str, str]) -> str:
        epc = form.get("epc", "")
        await self.hub.store.forget_unlinked(epc)
        self.hub.mark_dirty()
        return f"Removed {epc} from the unlinked list"

    async def _dismiss(self, form: dict[str, str]) -> str:
        barcode = form.get("barcode", "")
        await self.hub.store.remove_needs_product(barcode)
        return f"Removed barcode {barcode}"

    async def _retry(self, form: dict[str, str]) -> str:
        op_id = int(form.get("op", "0") or 0)
        if await self.hub.store.retry_failed(op_id):
            self.hub.worker.wake()
            self.hub.mark_dirty()
            return f"Op {op_id} queued again"
        return f"Op {op_id} is not failed"

    async def _consume_now(self, form: dict[str, str]) -> str:
        op = await self.hub.worker.consume_now(int(form.get("op", "0") or 0))
        self.hub.mark_dirty()
        return f"Consumed one {op.product_name} in Grocy (op {op.id})"

    async def _dismiss_op(self, form: dict[str, str]) -> str:
        op_id = int(form.get("op", "0") or 0)
        if await self.hub.worker.dismiss(op_id):
            self.hub.mark_dirty()
            return f"Op {op_id} dismissed (no change in Grocy)"
        return f"Op {op_id} is not waiting for review"

    async def _sweep(self, form: dict[str, str]) -> str:
        if self.hub.request_sweep():
            return "Checking the pantry now"
        return "Not started: a check is already running, or reading is continuous"

    async def _resync(self, form: dict[str, str]) -> str:
        report = await self.hub.resync()
        if report.error:
            return f"Resync incomplete: {report.error}"
        return f"Resync: {len(report.differences)} difference(s) in {report.checked} product(s)"

    async def _set_door(self, form: dict[str, str]) -> str:
        message = await self.hub.set_door_entity(form.get("door_entity", ""))
        if "inverted" in form:
            await self._set_inverted(form)
        return message

    async def _set_inverted(self, form: dict[str, str]) -> str:
        value = form.get("inverted", "").strip().lower()
        if value not in ("true", "false", "on", "off", "1", "0", ""):
            raise ValueError(f"inverted must be true or false, got {value!r}")
        inverted = value in ("true", "on", "1")
        await self.hub.set_door_inverted(inverted)
        return "Open/closed reversed" if inverted else "Open/closed as reported"


SHELL = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="light dark">
<title>Pantry Hub</title>
<link rel="stylesheet" href="{base}/static/tokens.css?v={v}">
<link rel="stylesheet" href="{base}/static/app.css?v={v}">
<script type="module" src="{base}/static/app.js?v={v}"></script>
</head>
<body>
<div id="app" data-boot="{boot}" aria-busy="true">
<header class="top">
<h1 id="title">Pantry</h1>
<p class="fresh" id="fresh" aria-live="polite">Loading…</p>
</header>
<main id="view" tabindex="-1">
<div class="skeleton" aria-hidden="true"><div></div><div></div><div></div></div>
</main>
</div>
<noscript><p class="noscript">Pantry Hub needs JavaScript.</p></noscript>
</body>
</html>
"""
