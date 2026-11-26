from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
import re

import aiohttp
import pytest

from pantry_hub.store import Link
from pantry_hub.web import WebUI

from .conftest import FakeClock, free_port
from .fake_grocy import FakeGrocy
from .rig import Rig

EPC = "E28011700000020F1A2B3C09"


@pytest.fixture
async def rig(tmp_path: Path, grocy: FakeGrocy, clock: FakeClock) -> AsyncIterator[Rig]:
    r = Rig(tmp_path, grocy, None, clock, grocy_ui_url="https://ha.example/grocy")
    await r.start()
    yield r
    await r.stop()
    r.close()


async def serve(rig: Rig, allowed: list[str] | None) -> tuple[WebUI, str]:
    ui = WebUI(rig.h, allowed_peers=allowed)
    port = free_port()
    await ui.start("127.0.0.1", port)
    return ui, f"http://127.0.0.1:{port}"


def token(page: str) -> str:
    match = re.search(r'name="csrf" value="([^"]+)"', page)
    assert match
    return match.group(1)


async def test_page_and_actions(rig: Rig, grocy: FakeGrocy) -> None:
    beans = grocy.add_product("Beans & <Co>", ["0001"])
    rice = grocy.add_product("Rice", ["0002"])
    assert rig.store is not None
    await rig.store.create_link(
        Link(EPC, beans.id, beans.name, rig.clock()), location_id=1
    )
    await rig.store.apply_arrival("UNLINKED01", rig.clock(), 1)
    await rig.store.add_needs_product("4242", "kitchen", rig.clock())
    ui, base = await serve(rig, ["127.0.0.1"])
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                base + "/", headers={"X-Ingress-Path": "/api/hassio_ingress/abc"}
            ) as r:
                page = await r.text()
            assert r.status == 200
            assert "Beans &amp; &lt;Co&gt;" in page  # escaped
            assert f"https://ha.example/grocy/product/{beans.id}" in page
            assert "UNLINKED01" in page and "4242" in page
            assert "<script" not in page and "http://cdn" not in page
            csrf = token(page)
            hdr = {"X-Ingress-Path": "/api/hassio_ingress/abc"}

            async def post(path: str, **data: str) -> str:
                async with s.post(
                    base + path,
                    data={"csrf": csrf, **data},
                    headers=hdr,
                    allow_redirects=False,
                ) as resp:
                    assert resp.status == 303
                    assert resp.headers["Location"] == "/api/hassio_ingress/abc/"
                async with s.get(base + "/") as resp:
                    return await resp.text()

            page = await post("/relink", epc=EPC, target="0002")
            assert f"Linked {EPC} to Rice" in page
            page = await post("/relink", epc=EPC, target=f"#{beans.id}")
            assert "to Beans" in page
            page = await post("/relink", epc="zz", target="1")
            assert "Failed: not a valid EPC" in page
            page = await post("/relink", epc=EPC, target="")
            assert "Failed" in page
            page = await post("/relink", epc=EPC, target="nope")
            assert "Failed: Grocy has no product" in page
            page = await post("/unlink", epc=EPC)
            assert f"Unlinked {EPC}" in page
            page = await post("/unlink", epc=EPC)
            assert "was not linked" in page
            page = await post("/forget", epc="UNLINKED01")
            assert "Removed UNLINKED01" in page
            page = await post("/dismiss", barcode="4242")
            assert "Removed barcode 4242" in page
            page = await post("/retry", op="999")
            assert "is not failed" in page
            page = await post("/resync")
            assert "Resync:" in page
            # Stale token.
            async with s.post(
                base + "/unlink", data={"csrf": "bad", "epc": EPC}
            ) as resp:
                assert resp.status == 403
        assert rice.id
    finally:
        await ui.stop()


async def test_retry_failed_op(rig: Rig, grocy: FakeGrocy) -> None:
    beans = grocy.add_product("Beans")
    assert rig.store is not None
    await rig.store.create_link(
        Link(EPC, beans.id, "Beans", rig.clock()), location_id=1
    )
    res = await rig.store.apply_arrival(EPC, rig.clock(), 1)
    assert res.add_op_id is not None
    await rig.store.mark_failed(res.add_op_id, "rejected", rig.clock())
    ui, base = await serve(rig, ["*"])
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(base + "/") as r:
                page = await r.text()
            assert "rejected" in page
            async with s.post(
                base + "/retry", data={"csrf": token(page), "op": str(res.add_op_id)}
            ) as r:
                page = await r.text()
            assert "queued again" in page
    finally:
        await ui.stop()


async def test_non_ingress_peer_refused(rig: Rig) -> None:
    ui, base = await serve(rig, None)  # default: only 172.30.32.2
    try:
        async with aiohttp.ClientSession() as s, s.get(base + "/") as r:
            assert r.status == 403
    finally:
        await ui.stop()


async def test_resync_error_and_report_rendering(rig: Rig, grocy: FakeGrocy) -> None:
    beans = grocy.add_product("Beans")
    assert rig.store is not None
    await rig.store.create_link(
        Link(EPC, beans.id, "Beans", rig.clock()), location_id=1
    )
    assert rig.client is not None
    await rig.client.add(beans.id, location_id=1, best_before=None, note="manual")
    await rig.h.resync()
    ui, base = await serve(rig, ["*"])
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(base + "/") as r:
                page = await r.text()
            assert "1 difference(s)" in page and "Grocy (pantry)" in page
            grocy.down = True
            async with s.post(base + "/resync", data={"csrf": token(page)}) as r:
                page = await r.text()
            assert "Resync incomplete" in page
    finally:
        await ui.stop()


def test_ago() -> None:
    import time

    from pantry_hub.web import _ago

    now = time.time()
    assert _ago(None) == "never"
    assert _ago(now - 10).endswith("s ago")
    assert _ago(now - 600).endswith("min ago")
    assert _ago(now - 7200).endswith("h ago")
    assert _ago(now - 400000).endswith("d ago")
