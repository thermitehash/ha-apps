"""Web UI (v0.4.0): HTML shell, static assets, JSON actions, pictures."""

from __future__ import annotations

from collections.abc import AsyncIterator
import html
import json
from pathlib import Path
import re

import aiohttp
import pytest

from pantry_hub.store import Link
from pantry_hub.web import STATIC_CACHE, WebUI

from .conftest import FakeClock, free_port
from .fake_grocy import FakeGrocy
from .rig import Rig
from .web_client import WebClient

EPC = "E28011700000020F1A2B3C09"
PREFIX = "/api/hassio_ingress/abc"
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c63f8cfc0f01f0005000201a5f6e3c50000000049454e44ae426082"
)


@pytest.fixture
async def rig(tmp_path: Path, grocy: FakeGrocy, clock: FakeClock) -> AsyncIterator[Rig]:
    r = Rig(tmp_path, grocy, None, clock, grocy_ui_url="https://ha.example/grocy")
    await r.start()
    yield r
    await r.stop()
    r.close()


# Shell and static assets ---------------------------------------------------------


async def test_shell_builds_every_url_from_the_ingress_prefix(rig: Rig) -> None:
    async with WebClient(rig.h, prefix=PREFIX) as c:
        shell = await c.text("/")
        v = rig.h.version
        assert f'href="{PREFIX}/static/tokens.css?v={v}"' in shell
        assert f'href="{PREFIX}/static/app.css?v={v}"' in shell
        assert f'src="{PREFIX}/static/app.js?v={v}"' in shell
        assert c.boot == {"base": PREFIX, "csrf": c.csrf, "version": v}
        assert len(c.csrf) > 20
        # No third-party hosts (fonts, CDNs): the page works offline.
        assert not re.search(r"https?://", shell)
        assert "Pantry Hub" in shell and 'aria-busy="true"' in shell


async def test_hostile_ingress_header_is_escaped(rig: Rig) -> None:
    async with WebClient(rig.h, prefix='/x"><script>alert(1)</script>') as c:
        shell = await c.text("/")
    assert "<script>alert(1)" not in shell
    m = re.search(r'data-boot="([^"]*)"', shell)
    assert m
    assert json.loads(html.unescape(m.group(1)))["base"].startswith('/x">')


async def test_static_assets_versioned_and_cached(rig: Rig) -> None:
    async with WebClient(rig.h) as c:
        assert c.session is not None
        for name, ctype in (
            ("app.js", "text/javascript"),
            ("api.js", "text/javascript"),
            ("views.js", "text/javascript"),
            ("app.css", "text/css"),
            ("tokens.css", "text/css"),
        ):
            async with c.session.get(f"{c.base}/static/{name}?v=1") as r:
                assert r.status == 200, name
                assert r.content_type == ctype
                assert r.headers["Cache-Control"] == STATIC_CACHE
                body = await r.text()
            assert "__VERSION__" not in body, name
        async with c.session.get(f"{c.base}/static/app.js") as r:
            assert f'./api.js?v={rig.h.version}"' in await r.text()
        for bad in ("../hub.py", "nope.js", ".hidden.css", "state.py"):
            async with c.session.get(f"{c.base}/static/{bad}") as r:
                assert r.status == 404, bad


async def test_tokens_shared_with_laundry_hub(rig: Rig) -> None:
    css = (
        Path(__file__).resolve().parents[1] / "src/pantry_hub/web/static/tokens.css"
    ).read_text()
    for token in (
        "--ink: #1c1b19",
        "--muted: #5c5953",
        "--ok: #2e7d4f",
        "--warn: #9a6412",
        "--problem: #b3261e",
        "--problem-bg: #fbe9e7",
        "--r-sm: 12px",
        "--r-md: 16px",
        "--r-pill: 999px",
        "--fs-xl: 28px",
        "--sp-6: 32px",
        "prefers-color-scheme: dark",
        "ha-laundry-hub",
    ):
        assert token in css, token
    app = (
        Path(__file__).resolve().parents[1] / "src/pantry_hub/web/static/app.css"
    ).read_text()
    for token in (
        "--ph-accent: #a9521f",
        "--ph-pressed: #8a4218",
        "--ph-sage: #5e7a4f",
        "--ph-sage-tint: #eef2ea",
        "--ph-clay-tint: #f6e6da",
        "--ph-surface: #faf6f1",
        "--ph-border: #eadfd3",
        "prefers-reduced-motion",
    ):
        assert token in app, token


async def test_non_ingress_peer_refused(rig: Rig) -> None:
    ui = WebUI(rig.h)  # default: only 172.30.32.2
    port = free_port()
    await ui.start("127.0.0.1", port)
    try:
        async with (
            aiohttp.ClientSession() as s,
            s.get(f"http://127.0.0.1:{port}/") as r,
        ):
            assert r.status == 403
    finally:
        await ui.stop()


# Actions (JSON) ------------------------------------------------------------------


async def test_every_action_answers_json(rig: Rig, grocy: FakeGrocy) -> None:
    beans = grocy.add_product("Beans & <Co>", ["0001"])
    grocy.add_product("Rice", ["0002"])
    assert rig.store is not None
    await rig.store.create_link(
        Link(EPC, beans.id, beans.name, rig.clock()), location_id=1
    )
    await rig.store.apply_arrival("UNLINKED01", rig.clock(), 1)
    await rig.store.add_needs_product("4242", "kitchen", rig.clock())
    async with WebClient(rig.h, prefix=PREFIX) as c:
        state = await c.state()
        assert [lk["product_name"] for lk in state["links"]] == ["Beans & <Co>"]
        assert [u["epc"] for u in state["unlinked"]] == ["UNLINKED01"]
        assert state["needs_product"][0]["barcode"] == "4242"
        assert state["grocy"]["ui_url"] == "https://ha.example/grocy"

        async def ok(path: str, fragment: str, **fields: str) -> None:
            res = await c.post(path, **fields)
            assert res["ok"] is True, res
            assert fragment in res["message"], res

        async def fails(path: str, fragment: str, **fields: str) -> None:
            res = await c.post(path, **fields)
            assert res["ok"] is False, res
            assert fragment in res["message"], res

        await ok("/relink", f"Linked {EPC} to Rice", epc=EPC, target="0002")
        await ok("/relink", "to Beans", epc=EPC, target=f"#{beans.id}")
        await ok("/relink", "already in stock", epc=EPC, target="0002", in_stock="on")
        await fails("/relink", "not a valid EPC", epc="zz", target="1")
        await fails("/relink", "enter a barcode", epc=EPC, target="")
        await fails("/relink", "Grocy has no product", epc=EPC, target="nope")
        await ok("/unlink", f"Unlinked {EPC}", epc=EPC)
        await ok("/unlink", "was not linked", epc=EPC)
        await ok("/forget", "Removed UNLINKED01", epc="UNLINKED01")
        await ok("/dismiss", "Removed barcode 4242", barcode="4242")
        await ok("/retry", "is not failed", op="999")
        await ok("/dismiss-op", "not waiting for review", op="999")
        await fails("/consume-now", "Failed:", op="999")
        await ok("/resync", "Resync:")
        await ok("/sweep", "Not started")  # continuous mode has no sweeps
        state = await c.state()
        assert state["links"] == [] and state["unlinked"] == []
        assert state["needs_product"] == []
        assert state["resync"]["checked_at"] is not None
        # Stale token: 403 with a JSON message, nothing done.
        assert c.session is not None
        async with c.session.post(
            c.base + "/forget", data={"csrf": "bad", "epc": "X"}
        ) as r:
            assert r.status == 403
            assert (await r.json())["message"] == "Stale page; reload it"
        # JSON body with the token in the header works too.
        async with c.session.post(
            c.base + "/resync",
            json={"x": None},
            headers={"X-CSRF-Token": c.csrf},
        ) as r:
            assert (await r.json())["ok"] is True
        async with c.session.post(
            c.base + "/resync",
            data="[1]",
            headers={"X-CSRF-Token": c.csrf, "Content-Type": "application/json"},
        ) as r:
            assert r.status == 400


async def test_retry_failed_op(rig: Rig, grocy: FakeGrocy) -> None:
    beans = grocy.add_product("Beans")
    assert rig.store is not None
    await rig.store.create_link(
        Link(EPC, beans.id, "Beans", rig.clock()), location_id=1
    )
    res = await rig.store.apply_arrival(EPC, rig.clock(), 1)
    assert res.add_op_id is not None
    await rig.store.mark_failed(res.add_op_id, "rejected", rig.clock())
    async with WebClient(rig.h) as c:
        state = await c.state()
        assert state["failed_ops"] == [
            {
                "id": res.add_op_id,
                "kind": "add",
                "product": "Beans",
                "epc": EPC,
                "error": "rejected",
                "needs_review": False,
            }
        ]
        assert state["health"]["title"] == "Reader offline"
        titles = [i["title"] for i in state["health"]["issues"]]
        assert "1 Grocy update failed" in titles
        reply = await c.post("/retry", op=res.add_op_id)
        assert "queued again" in reply["message"]


async def test_resync_error_and_report(rig: Rig, grocy: FakeGrocy) -> None:
    beans = grocy.add_product("Beans")
    assert rig.store is not None
    await rig.store.create_link(
        Link(EPC, beans.id, "Beans", rig.clock()), location_id=1
    )
    assert rig.client is not None
    await rig.client.add(beans.id, location_id=1, best_before=None, note="manual")
    await rig.h.resync()
    async with WebClient(rig.h) as c:
        report = (await c.state())["resync"]
        assert report["checked"] == 1
        assert report["differences"][0]["product"] == "Beans"
        grocy.down = True
        reply = await c.post("/resync")
        assert "Resync incomplete" in reply["message"]
        assert (await c.state())["resync"]["error"]


# Product pictures ----------------------------------------------------------------


async def test_product_picture_proxied_or_204(rig: Rig, grocy: FakeGrocy) -> None:
    beans = grocy.add_product("Beans")
    beans.picture = PNG
    oats = grocy.add_product("Oats")
    async with WebClient(rig.h) as c:
        assert c.session is not None
        async with c.session.get(f"{c.base}/api/product-picture/{beans.id}") as r:
            assert r.status == 200
            assert r.content_type == "image/png"
            assert r.headers["Cache-Control"] == "private, max-age=3600"
            assert await r.read() == PNG
        async with c.session.get(f"{c.base}/api/product-picture/{oats.id}") as r:
            assert r.status == 204  # no picture: the page shows an icon
            assert r.headers["Cache-Control"] == "no-store"
        async with c.session.get(f"{c.base}/api/product-picture/nope") as r:
            assert r.status == 404
        # Cached: Grocy going down doesn't break a picture already fetched.
        grocy.down = True
        async with c.session.get(f"{c.base}/api/product-picture/{beans.id}") as r:
            assert r.status == 200
        async with c.session.get(f"{c.base}/api/product-picture/9999") as r:
            assert r.status == 204
