"""Minimal server-rendered ingress UI (no build step, no external assets).

Home Assistant ingress authenticates the user; this server only accepts
connections from the Supervisor's ingress proxy (172.30.32.2) unless told
otherwise for local development. Every form carries a per-process CSRF token.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
import html
import logging
import secrets
import time
from typing import TYPE_CHECKING
from urllib.parse import quote

from aiohttp import web

from ..grocy import GrocyError
from ..linking import normalize_epc
from ..store import iso

if TYPE_CHECKING:
    from ..hub import Hub

_LOGGER = logging.getLogger(__name__)

INGRESS_PROXY = "172.30.32.2"
HUB_KEY = web.AppKey("hub", object)
TOKEN_KEY = web.AppKey("csrf", str)
ALLOW_KEY = web.AppKey("allow", frozenset)

_CSS = """
:root{--bg:#fff;--fg:#1c1c1c;--muted:#666;--line:#ddd;--ok:#1b7f3b;--bad:#b3261e;--warn:#9a6700;--accent:#03a9f4}
@media (prefers-color-scheme:dark){:root{--bg:#111;--fg:#e8e8e8;--muted:#9a9a9a;--line:#333;--ok:#5bd68a;--bad:#ff8a80;--warn:#f2c14e}}
body{font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--fg);margin:0 auto;padding:16px;max-width:1100px}
h1{font-size:20px;margin:0 0 12px}h2{font-size:16px;margin:24px 0 8px}
.cards{display:flex;flex-wrap:wrap;gap:8px}.card{border:1px solid var(--line);border-radius:8px;padding:8px 12px;min-width:120px}
.card b{display:block;font-size:20px}.ok{color:var(--ok)}.bad{color:var(--bad)}.warn{color:var(--warn)}.muted{color:var(--muted)}
.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
td code{font-size:12px;word-break:break-all}form.inline{display:inline}button{font:inherit;padding:2px 8px;cursor:pointer}
input{font:inherit;padding:2px 4px;max-width:150px}a{color:var(--accent)}.msg{padding:8px 12px;border-radius:6px;border:1px solid var(--line);margin:8px 0}
"""


def _e(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _ago(ts: float | None) -> str:
    if not ts:
        return "never"
    secs = max(0, int(time.time() - ts))
    if secs < 90:
        return f"{secs} s ago"
    if secs < 5400:
        return f"{secs // 60} min ago"
    if secs < 172800:
        return f"{secs // 3600} h ago"
    return f"{secs // 86400} d ago"


def _status(ok: bool | None, yes: str, no: str, unknown: str = "unknown") -> str:
    if ok is None:
        return f'<span class="warn">{unknown}</span>'
    return f'<span class="ok">{yes}</span>' if ok else f'<span class="bad">{no}</span>'


def _rows(rows: Iterable[str], cols: int, empty: str) -> str:
    body = "".join(rows)
    return body or f'<tr><td colspan="{cols}" class="muted">{empty}</td></tr>'


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
        self.app = web.Application(middlewares=[self._guard])
        self.app[ALLOW_KEY] = allow
        self.app.router.add_get("/", self.index)
        self.app.router.add_post("/unlink", self._form(self._unlink))
        self.app.router.add_post("/relink", self._form(self._relink))
        self.app.router.add_post("/forget", self._form(self._forget))
        self.app.router.add_post("/dismiss", self._form(self._dismiss))
        self.app.router.add_post("/retry", self._form(self._retry))
        self.app.router.add_post("/consume-now", self._form(self._consume_now))
        self.app.router.add_post("/dismiss-op", self._form(self._dismiss_op))
        self.app.router.add_post("/resync", self._form(self._resync))
        self.app.router.add_post("/sweep", self._form(self._sweep))
        self._runner: web.AppRunner | None = None
        self._flash: str | None = None

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

    def _form(
        self, action: Callable[[dict[str, str]], Awaitable[str]]
    ) -> Callable[[web.Request], Awaitable[web.StreamResponse]]:
        async def handler(request: web.Request) -> web.StreamResponse:
            data = await request.post()
            form = {k: str(v) for k, v in data.items()}
            if not secrets.compare_digest(form.get("csrf", ""), self.token):
                raise web.HTTPForbidden(text="Stale form; reload the page")
            try:
                self._flash = await action(form)
            except (GrocyError, ValueError) as err:
                self._flash = f"Failed: {err}"
            base = request.headers.get("X-Ingress-Path", "").rstrip("/")
            raise web.HTTPSeeOther(f"{base}/")

        return handler

    # Actions ---------------------------------------------------------------------

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
        in_stock = form.get("in_stock") == "on"
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
            return "Sweep requested"
        return "Sweep not started (one is running, or reading_mode is continuous)"

    async def _resync(self, form: dict[str, str]) -> str:
        report = await self.hub.resync()
        if report.error:
            return f"Resync incomplete: {report.error}"
        return f"Resync: {len(report.differences)} difference(s) in {report.checked} product(s)"

    # Page ------------------------------------------------------------------------

    def _button(self, action: str, label: str, **fields: object) -> str:
        hidden = "".join(
            f'<input type="hidden" name="{_e(k)}" value="{_e(v)}">'
            for k, v in fields.items()
        )
        return (
            f'<form class="inline" method="post" action="{action}">'
            f'<input type="hidden" name="csrf" value="{self.token}">{hidden}'
            f"<button>{_e(label)}</button></form>"
        )

    def _relink_form(self, epc: str) -> str:
        return (
            '<form class="inline" method="post" action="relink">'
            f'<input type="hidden" name="csrf" value="{self.token}">'
            f'<input type="hidden" name="epc" value="{_e(epc)}">'
            '<input name="target" placeholder="barcode or #id" aria-label="barcode or product id">'
            '<label title="Grocy already counts this unit: skip the add for its current '
            'or next arrival"><input type="checkbox" name="in_stock"> already in stock</label> '
            "<button>Link</button></form>"
        )

    async def index(self, request: web.Request) -> web.Response:
        """Render the status page."""
        hub = self.hub
        opts = hub.options
        grocy = opts.grocy_browser_url
        await hub.refresh_counts()
        state = hub.state()
        links = await hub.store.links()
        unlinked = await hub.store.unlinked()
        needs = await hub.store.needs_product()
        failed = await hub.store.ops("failed", limit=50)
        rows = await hub.store.presence_rows()
        last_seen = {r.epc: r.last_seen for r in rows}
        in_pantry = set(hub.machine.in_pantry())
        mqtt_ok = hub.mqtt.connected if hub.mqtt is not None else None

        flash, self._flash = self._flash, None
        cards = [
            ("Reader", _status(state["reader_online"], "online", "offline")),
            ("Grocy", _status(hub.worker.grocy_ok, "online", "error", "not used yet")),
            ("MQTT", _status(mqtt_ok, "connected", "disconnected", "off")),
            ("Present tags", f"<b>{state['present_tags']}</b>"),
            ("Unlinked present", f"<b>{state['unlinked_tags']}</b>"),
            ("Reader activity", f"<b>{_e(state['activity'])}</b>"),
            ("Grocy queue", f"<b>{state['grocy_queue']}</b>"),
            ("Failed ops", f"<b>{state['grocy']['failed']}</b>"),
        ]
        cards_html = "".join(
            f'<div class="card"><span class="muted">{label}</span><br>{value}</div>'
            for label, value in cards
        )
        link_rows = (
            "<tr>"
            f"<td><code>{_e(lk.epc)}</code>{' <span class=ok>●</span>' if lk.epc in in_pantry else ''}</td>"
            f'<td><a href="{_e(grocy)}/product/{lk.product_id}" target="_blank" rel="noopener">{_e(lk.product_name)}</a></td>'
            f"<td>{_e(iso(lk.linked_at))}"
            + (
                f' <span class="muted">(in stock: no add for arrival {lk.skip_add_cycle})</span>'
                if lk.skip_add_cycle
                else ""
            )
            + f"</td><td>{_ago(last_seen.get(lk.epc))}</td>"
            f"<td>{self._button('unlink', 'Unlink', epc=lk.epc)} {self._relink_form(lk.epc)}</td>"
            "</tr>"
            for lk in links
        )
        unlinked_rows = (
            "<tr>"
            f"<td><code>{_e(u.epc)}</code>{' <span class=ok>●</span>' if u.epc in in_pantry else ''}</td>"
            f"<td>{_ago(u.last_seen)}</td><td>{u.arrivals}</td>"
            f"<td>{self._relink_form(u.epc)} {self._button('forget', 'Forget', epc=u.epc)} "
            f'<a href="{_e(grocy)}/products" target="_blank" rel="noopener">Grocy products</a></td>'
            "</tr>"
            for u in unlinked
        )
        needs_rows = (
            "<tr>"
            f"<td><code>{_e(n.barcode)}</code></td><td>{_e(n.station)}</td>"
            f"<td>{_ago(n.last_seen)}</td><td>{n.scans}</td>"
            f'<td><a href="{_e(grocy)}/product/new" target="_blank" rel="noopener">Create in Grocy</a> '
            f"{self._button('dismiss', 'Dismiss', barcode=n.barcode)}</td>"
            "</tr>"
            for n in needs
        )
        failed_rows = (
            "<tr>"
            f"<td>{op.id}</td><td>{_e(op.kind)}</td><td>{_e(op.product_name)}</td>"
            f"<td><code>{_e(op.epc)}</code></td><td>{_e(op.last_error)}</td>"
            + (
                f"<td><b>Review:</b> {self._button('consume-now', 'Consume now', op=op.id)} "
                f"{self._button('dismiss-op', 'Dismiss', op=op.id)}</td>"
                if op.needs_review
                else f"<td>{self._button('retry', 'Retry', op=op.id)}</td>"
            )
            + "</tr>"
            for op in failed
        )
        resync = hub.last_resync
        resync_html = '<p class="muted">Not run yet.</p>'
        if resync is not None:
            diff_rows = "".join(
                f"<tr><td>{_e(d['product'])}</td><td>{d['present_tags']}</td><td>{d['grocy_amount']:g}</td></tr>"
                for d in resync.differences
            )
            resync_html = (
                f"<p>{_e(iso(resync.checked_at))}: {resync.checked} product(s), "
                f"{len(resync.differences)} difference(s)"
                + (
                    f' <span class="bad">({_e(resync.error)})</span>'
                    if resync.error
                    else ""
                )
                + "</p>"
                + (
                    '<div class="scroll"><table><tr><th>Product</th><th>Tags present</th><th>Grocy (pantry)</th></tr>'
                    + diff_rows
                    + "</table></div>"
                    if diff_rows
                    else ""
                )
                + (
                    f'<p class="warn">{len(resync.review_ops)} consume(s) waiting for review '
                    "(Grocy may be one unit high for: "
                    + ", ".join(_e(r["product"]) for r in resync.review_ops)
                    + "). See Failed Grocy operations.</p>"
                    if resync.review_ops
                    else ""
                )
            )
        page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pantry Hub</title><style>{_CSS}</style></head><body>
<h1>Pantry Hub <span class="muted">{_e(hub.version)}</span></h1>
{f'<div class="msg">{_e(flash)}</div>' if flash else ""}
<div class="cards">{cards_html}</div>
<p class="muted">Reader {_e(opts.reader_host)} · last datagram {_e(state["reader"]["last_datagram"] or "never")} ·
bad frames {state["reader"]["bad_frames"]} · dropped sources {state["reader"]["dropped_sources"]} ·
door {_e(state["door"])}{f" · Grocy error: {_e(state['grocy']['last_error'])}" if state["grocy"]["last_error"] else ""}</p>
<p class="muted">Reading mode {_e(state["reading"]["mode"])} · last sweep {_e(state["last_sweep"] or "never")} ·
sweeps completed {state["reading"]["sweeps_completed"]} (aborted {state["reading"]["sweeps_aborted"]}) ·
reader id {_e(state["reading"]["reader_id"])}{" · <b>departures paused until 2 sweeps complete</b>" if not state["reading"]["departures_allowed"] else ""}
{self._button("sweep", "Sweep now") if hub.door_mode else ""}</p>
<h2>Links ({len(links)})</h2>
<div class="scroll"><table><tr><th>EPC</th><th>Product</th><th>Linked</th><th>Last seen</th><th></th></tr>
{_rows(link_rows, 5, "No links yet. Use the linking station: double-click its button, scan a barcode, hold the tag near the reader.")}</table></div>
<h2>Unlinked tags ({len(unlinked)})</h2>
<div class="scroll"><table><tr><th>EPC</th><th>Last seen</th><th>Arrivals</th><th></th></tr>
{_rows(unlinked_rows, 4, "None.")}</table></div>
<h2>Barcodes that need a Grocy product ({len(needs)})</h2>
<div class="scroll"><table><tr><th>Barcode</th><th>Station</th><th>Last scan</th><th>Scans</th><th></th></tr>
{_rows(needs_rows, 5, "None.")}</table></div>
<h2>Failed Grocy operations ({len(failed)})</h2>
<div class="scroll"><table><tr><th>#</th><th>Kind</th><th>Product</th><th>EPC</th><th>Error</th><th></th></tr>
{_rows(failed_rows, 6, "None.")}</table></div>
<h2>Resync</h2>{resync_html}
{self._button("resync", "Run resync now")}
<p class="muted">● = in the pantry now. Resync only reports differences; fix them in Grocy.
Relink takes a barcode or a Grocy product id written as <code>#12</code>.</p>
</body></html>"""
        return web.Response(text=page, content_type="text/html")


def grocy_product_url(base: str, product_id: int) -> str:
    """Grocy product page URL."""
    return f"{base}/product/{quote(str(product_id))}"
