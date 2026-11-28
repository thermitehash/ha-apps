"""v0.4.1: product pictures from Kroger Restock, stored in Grocy."""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator
import io
import logging
from pathlib import Path
from typing import Any

from aiohttp import web
from PIL import Image
import pytest

from pantry_hub.ha_api import HomeAssistantApi, rest_url_from_ws
from pantry_hub.pictures import (
    MISS_TTL_S,
    BadImage,
    PictureService,
    resize_jpeg,
)
from pantry_hub.store import Store

from .conftest import FakeClock, eventually
from .fake_grocy import FakeGrocy
from .fake_ha import TOKEN, FakeHomeAssistant
from .fake_kroger import FakeKroger
from .ingress_proxy import IngressProxy
from .rig import Rig
from .web_client import WebClient

EPC = "E28011700000020F1A2B3C09"
SECRET = "sig=s3cr3t-token"


def png(w: int, h: int, mode: str = "RGB") -> bytes:
    out = io.BytesIO()
    Image.new(
        mode, (w, h), (200, 30, 30, 128) if mode == "RGBA" else (200, 30, 30)
    ).save(out, "PNG")
    return out.getvalue()


class ImageHost:
    """Serves test images: /ok.png, /big, /chunked-big, /html, /missing."""

    def __init__(self) -> None:
        self.hits: list[str] = []
        self.fail_next = 0
        self._runner: web.AppRunner | None = None
        self.base = ""

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/{name}", self.serve)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        self.base = f"http://127.0.0.1:{port}"

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    def url(self, name: str) -> str:
        return f"{self.base}/{name}?{SECRET}"

    async def serve(self, request: web.Request) -> web.StreamResponse:
        name = request.match_info["name"]
        self.hits.append(name)
        if self.fail_next:
            self.fail_next -= 1
            return web.Response(status=503)
        if name == "ok.png":
            return web.Response(body=png(1200, 800), content_type="image/png")
        if name == "alpha.png":
            return web.Response(body=png(300, 300, "RGBA"), content_type="image/png")
        if name == "big":
            return web.Response(
                body=b"\0" * (5 * 1024 * 1024 + 1), content_type="image/jpeg"
            )
        if name == "chunked-big":
            resp = web.StreamResponse(headers={"Content-Type": "image/jpeg"})
            resp.enable_chunked_encoding()
            await resp.prepare(request)
            for _ in range(6):
                await resp.write(b"\0" * (1024 * 1024))
            await resp.write_eof()
            return resp
        if name == "html":
            return web.Response(text="<html>nope</html>", content_type="text/html")
        if name == "notimage":
            return web.Response(body=b"not an image", content_type="image/png")
        return web.Response(status=404)


@pytest.fixture
async def host() -> AsyncIterator[ImageHost]:
    h = ImageHost()
    await h.start()
    yield h
    await h.stop()


def kroger(ha: FakeHomeAssistant, answers: dict[str, dict[str, Any]]) -> FakeKroger:
    """Kroger Restock v0.17.2 knowing the products in ``answers`` (by barcode)."""
    k = FakeKroger(ha)
    for code, fields in answers.items():
        k.add(code, **{f: v for f, v in fields.items() if f != "found"})
    return k


@pytest.fixture
async def rig(
    tmp_path: Path, grocy: FakeGrocy, clock: FakeClock, ha: FakeHomeAssistant
) -> AsyncIterator[Rig]:
    r = Rig(tmp_path, grocy, None, clock)
    yield r
    await r.stop()
    r.close()


async def start(rig: Rig, ha: FakeHomeAssistant, **kw: Any) -> None:
    await rig.start(
        ha_url=ha.url,
        ha_token=TOKEN,
        picture_retry_delays=kw.pop("picture_retry_delays", (0.01, 0.01)),
        picture_spacing_s=kw.pop("picture_spacing_s", 0.0),
        ha_backoff_s=0.05,
        **kw,
    )


def jpeg_size(data: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(data)) as img:
        assert img.format == "JPEG"
        return img.size


# Pure pieces ---------------------------------------------------------------------


def test_rest_url_from_ws() -> None:
    assert rest_url_from_ws("ws://supervisor/core/websocket") == (
        "http://supervisor/core/api"
    )
    assert rest_url_from_ws("wss://ha.local:8123/api/websocket") == (
        "https://ha.local:8123/api"
    )


def test_resize_keeps_aspect_never_upscales_and_flattens_alpha() -> None:
    assert jpeg_size(resize_jpeg(png(1200, 800))) == (600, 400)
    assert jpeg_size(resize_jpeg(png(500, 2000))) == (150, 600)
    assert jpeg_size(resize_jpeg(png(100, 50))) == (100, 50)
    assert jpeg_size(resize_jpeg(png(64, 64, "RGBA"))) == (64, 64)
    with pytest.raises(BadImage):
        resize_jpeg(b"definitely not an image")


# Link → picture ------------------------------------------------------------------


async def test_link_uploads_picture_to_grocy(
    rig: Rig, grocy: FakeGrocy, ha: FakeHomeAssistant, host: ImageHost
) -> None:
    beans = grocy.add_product("0001", ["0001"])  # name = barcode placeholder
    kroger(
        ha,
        {
            "0001": {
                "found": True,
                "upc": "0001",
                "name": "Black Beans",
                "brand": "Kroger",
                "size": "15 oz",
                "image_url": host.url("ok.png"),
            }
        },
    )
    await start(rig, ha)
    await rig.h.relink(EPC, barcode="0001")
    assert rig.h.pictures is not None
    await rig.h.pictures.wait_idle()
    # Only the barcode went to Home Assistant.
    assert ha.rest("kroger_restock", "lookup_product") == [{"upc": "0001"}]
    name = f"pantryhub_{beans.id}.jpg"
    assert jpeg_size(grocy.files[name]) == (600, 400)
    assert beans.picture_file_name == name
    # Empty name (the barcode placeholder) and description filled in.
    assert beans.name == "Black Beans"
    assert beans.description == "Kroger · 15 oz"
    assert rig.h.pictures.versions[beans.id] == 1
    # The text fill, then the upload, then the product points at the file.
    puts = [(p, body) for m, p, body in grocy.calls if m == "PUT"]
    encoded = base64.b64encode(name.encode()).decode()
    assert [p for p, _ in puts] == [
        f"/api/objects/products/{beans.id}",
        f"/api/files/productpictures/{encoded}",
        f"/api/objects/products/{beans.id}",
    ]
    assert puts[0][1] == {"name": "Black Beans", "description": "Kroger · 15 oz"}
    assert puts[2][1] == {"picture_file_name": name}


async def test_user_name_and_pictures_left_alone(
    rig: Rig, grocy: FakeGrocy, ha: FakeHomeAssistant, host: ImageHost
) -> None:
    mine = grocy.add_product("Grandma's beans", ["0001"])
    mine.picture_file_name = "my-photo.png"
    named = grocy.add_product("Our rice", ["0002"])
    kroger(
        ha,
        {
            b: {"found": True, "name": "Kroger thing", "image_url": host.url("ok.png")}
            for b in ("0001", "0002")
        },
    )
    await start(rig, ha)
    await rig.h.relink(EPC, barcode="0001")
    await rig.h.relink("E28011700000020F1A2B3C10", barcode="0002")
    assert rig.h.pictures is not None
    await rig.h.pictures.wait_idle()
    assert mine.picture_file_name == "my-photo.png"  # never touched
    assert ha.rest("kroger_restock", "lookup_product") == [{"upc": "0002"}]
    assert named.name == "Our rice"  # a person's name stays
    assert named.picture_file_name == f"pantryhub_{named.id}.jpg"


async def test_missing_kroger_restock_skips_and_says_so(
    rig: Rig, grocy: FakeGrocy, ha: FakeHomeAssistant
) -> None:
    grocy.add_product("Beans", ["0001"])
    await start(rig, ha)
    await rig.h.relink(EPC, barcode="0001")
    assert rig.h.pictures is not None
    await rig.h.pictures.wait_idle()
    assert ha.rest_calls == []
    assert grocy.files == {}
    async with WebClient(rig.h) as c:
        state = await c.state()
    assert ["Product pictures", "Kroger Restock not found."] in state["diagnostics"]
    assert state["pictures"]["available"] is False
    assert state["just_bought"]["available"] is False


async def test_no_ha_api_means_no_picture_feature(
    tmp_path: Path, grocy: FakeGrocy, clock: FakeClock
) -> None:
    r = Rig(tmp_path, grocy, None, clock)
    await r.start()
    try:
        # Kroger parts off; Unload / Stock-take still work with Grocy alone.
        assert r.h.ha_api is None and r.h.ha_events is None
        assert r.h.scans is not None
        assert "Kroger Restock" in r.h.fetch_missing_pictures()
        async with WebClient(r.h) as c:
            state = await c.state()
        assert ["Product pictures", "Kroger Restock not found."] in state["diagnostics"]
    finally:
        await r.stop()
        r.close()


@pytest.mark.parametrize(
    ("name", "why"),
    [
        ("big", "larger than 5 MB"),
        ("chunked-big", "larger than 5 MB"),
        ("html", "sent text/html"),
        ("notimage", "not a readable image"),
        ("missing", "answered 404"),
    ],
)
async def test_bad_downloads_rejected_without_leaking_the_url(
    rig: Rig,
    grocy: FakeGrocy,
    ha: FakeHomeAssistant,
    host: ImageHost,
    caplog: pytest.LogCaptureFixture,
    name: str,
    why: str,
) -> None:
    beans = grocy.add_product("Beans", ["0001"])
    kroger(ha, {"0001": {"found": True, "image_url": host.url(name)}})
    await start(rig, ha)
    caplog.set_level(logging.INFO)
    await rig.h.relink(EPC, barcode="0001")
    assert rig.h.pictures is not None
    await rig.h.pictures.wait_idle()
    assert grocy.files == {} and beans.picture_file_name is None
    assert host.hits == [name]  # an unusable image is not retried
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any(why in w for w in warnings), warnings
    assert all(SECRET not in r.getMessage() for r in caplog.records)


async def test_non_http_url_rejected(
    tmp_path: Path, grocy: FakeGrocy, clock: FakeClock
) -> None:
    svc = PictureService(grocy, None, None, clock=clock)  # type: ignore[arg-type]
    for url in ("file:///etc/passwd", "ftp://x/y.png", "javascript:alert(1)"):
        with pytest.raises(BadImage, match="not http"):
            await svc.download(url)
    await svc.close()


async def test_transient_failures_retried_in_background(
    rig: Rig,
    grocy: FakeGrocy,
    ha: FakeHomeAssistant,
    host: ImageHost,
    caplog: pytest.LogCaptureFixture,
) -> None:
    beans = grocy.add_product("Beans", ["0001"])
    kroger(ha, {"0001": {"found": True, "image_url": host.url("ok.png")}})
    host.fail_next = 2  # image host down for two attempts
    await start(rig, ha)
    link = await rig.h.relink(EPC, barcode="0001")  # linking isn't blocked
    assert link.product_id == beans.id
    assert rig.h.pictures is not None
    await rig.h.pictures.wait_idle()
    assert host.hits == ["ok.png"] * 3
    assert beans.picture_file_name == f"pantryhub_{beans.id}.jpg"
    assert all(SECRET not in r.getMessage() for r in caplog.records)


async def test_not_found_remembered(
    rig: Rig, grocy: FakeGrocy, ha: FakeHomeAssistant
) -> None:
    beans = grocy.add_product("Beans", ["0001"])
    kroger(ha, {})
    await start(rig, ha)
    await rig.h.relink(EPC, barcode="0001")
    assert rig.h.pictures is not None and rig.store is not None
    await rig.h.pictures.wait_idle()
    assert await rig.store.picture_miss(beans.id) == rig.clock()


# Backfill ------------------------------------------------------------------------


async def test_backfill_throttled_with_progress_and_30_day_memory(
    tmp_path: Path, grocy: FakeGrocy, ha: FakeHomeAssistant, host: ImageHost
) -> None:
    clock = FakeClock()
    store = await Store.open(tmp_path / "db")
    from pantry_hub.grocy import GrocyClient

    from .fake_grocy import API_KEY

    client = GrocyClient(grocy.url, API_KEY)
    api = HomeAssistantApi(ha.rest_url, TOKEN)
    sleeps: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    a = grocy.add_product("A", ["1"])
    b = grocy.add_product("B", ["2"])
    c = grocy.add_product("C", ["3"])
    user = grocy.add_product("User", ["4"])
    user.picture_file_name = "user.jpg"
    stale = grocy.add_product("Stale", ["5"])
    stale.picture_file_name = f"pantryhub_{stale.id}.jpg"  # file gone from Grocy
    kroger(
        ha,
        {
            "1": {"found": True, "image_url": host.url("ok.png")},
            "3": {"found": True, "image_url": host.url("alpha.png")},
            "5": {"found": True, "image_url": host.url("ok.png")},
        },
    )
    svc = PictureService(client, api, store, clock=clock, sleep=fake_sleep)
    products = {a.id: "1", b.id: None, c.id: "3", user.id: "4", stale.id: None}
    try:
        assert svc.start_backfill(products)
        assert not svc.start_backfill(products)  # one at a time
        await svc.wait_idle()
        assert sleeps == [2.0, 2.0, 2.0]  # 4 lookups, 2 s apart
        assert svc.progress.text() == "4 of 4, 1 not found"
        assert [d["upc"] for d in ha.rest("kroger_restock", "lookup_product")] == [
            "1",
            "2",
            "3",
            "5",
        ]
        assert user.picture_file_name == "user.jpg"
        assert stale.picture_file_name == f"pantryhub_{stale.id}.jpg"
        assert f"pantryhub_{stale.id}.jpg" in grocy.files
        # B had no picture: not asked again within 30 days.
        ha.rest_calls.clear()
        sleeps.clear()
        assert svc.start_backfill(products)
        await svc.wait_idle()
        assert ha.rest_calls == []
        assert svc.progress.text() == "Every product already has a picture"
        clock.advance(MISS_TTL_S + 1)
        assert svc.start_backfill(products)
        await svc.wait_idle()
        assert [d["upc"] for d in ha.rest("kroger_restock", "lookup_product")] == ["2"]
    finally:
        await svc.close()
        await api.close()
        await client.close()
        await store.close()


async def test_backfill_from_web_and_mqtt(
    rig: Rig, grocy: FakeGrocy, ha: FakeHomeAssistant, host: ImageHost
) -> None:
    beans = grocy.add_product("Beans", ["0001"])
    kroger(ha, {"0001": {"found": True, "image_url": host.url("ok.png")}})
    host.fail_next = 99  # the link-time attempt fails
    await start(rig, ha, picture_retry_delays=())
    await rig.h.relink(EPC, barcode="0001")
    assert rig.h.pictures is not None
    await rig.h.pictures.wait_idle()
    assert beans.picture_file_name is None
    host.fail_next = 0
    async with WebClient(rig.h) as c:
        reply = await c.post("/pictures/fetch")
        assert reply["message"] == "Fetching missing pictures"
        await eventually(
            lambda: (
                rig.h.pictures is not None
                and rig.h.pictures.progress.finished_at is not None
            )
        )
        state = await c.state()
    assert state["pictures"]["text"] == "1 of 1"
    assert beans.picture_file_name == f"pantryhub_{beans.id}.jpg"


async def test_backfill_mqtt_command(
    tmp_path: Path,
    grocy: FakeGrocy,
    clock: FakeClock,
    ha: FakeHomeAssistant,
    broker: Any,
    host: ImageHost,
) -> None:
    from pantry_hub.discovery import PICTURES_COMMAND_TOPIC

    from .rig import Observer

    beans = grocy.add_product("Beans", ["0001"])
    kroger(ha, {"0001": {"found": True, "image_url": host.url("ok.png")}})
    r = Rig(tmp_path, grocy, broker, clock)
    obs = Observer(broker.port)
    await obs.start()
    try:
        await start(r, ha, picture_retry_delays=())
        assert r.store is not None
        from pantry_hub.store import Link

        await r.store.create_link(
            Link(EPC, beans.id, "Beans", clock(), barcode="0001"), location_id=1
        )
        await obs.publish(PICTURES_COMMAND_TOPIC, "PRESS")
        await eventually(lambda: beans.picture_file_name is not None)
    finally:
        await obs.stop()
        await r.stop()
        r.close()


# Proxy route ---------------------------------------------------------------------


async def test_picture_route_under_ingress_prefix(
    rig: Rig, grocy: FakeGrocy, ha: FakeHomeAssistant, host: ImageHost
) -> None:
    import aiohttp

    beans = grocy.add_product("Beans", ["0001"])
    kroger(ha, {"0001": {"found": True, "image_url": host.url("ok.png")}})
    await start(rig, ha)
    async with WebClient(rig.h) as c:
        proxy = IngressProxy(c.base)
        await proxy.start()
        try:
            async with aiohttp.ClientSession() as s:
                url = f"{proxy.url}api/picture/{beans.id}"
                async with s.get(url) as r:
                    assert r.status == 204  # no picture yet
                    assert r.headers["Cache-Control"] == "no-store"
                await rig.h.relink(EPC, barcode="0001")
                assert rig.h.pictures is not None
                await rig.h.pictures.wait_idle()
                # The version changed, so the 204 cached above is not reused.
                async with s.get(url + "?v=1") as r:
                    assert r.status == 200
                    assert r.headers["Cache-Control"] == "private, max-age=86400"
                    assert r.content_type == "image/jpeg"
                    assert (await r.read())[:2] == b"\xff\xd8"
                assert proxy.paths[-1] == f"/api/picture/{beans.id}"
                state = await c.state()
                assert state["pictures"]["versions"] == {str(beans.id): 1}
        finally:
            await proxy.stop()


# Kroger Restock v0.17.2 contract (0.4.2) -----------------------------------------


async def test_lookup_product_rejects_barcode_key(ha: FakeHomeAssistant) -> None:
    """0.4.1 sent {"barcode": ...}: Kroger Restock answers 400 (regression)."""
    from pantry_hub.ha_api import HomeAssistantApiError

    k = FakeKroger(ha)
    k.add("049000028911", name="Coca-Cola")
    api = HomeAssistantApi(ha.rest_url, TOKEN)
    try:
        with pytest.raises(HomeAssistantApiError, match="HTTP 400"):
            await api.call(
                "kroger_restock", "lookup_product", {"barcode": "049000028911"}
            )
        with pytest.raises(HomeAssistantApiError, match="HTTP 400"):
            await api.call(
                "kroger_restock",
                "lookup_product",
                {"upc": "049000028911", "product_id": "0004900002891"},
            )
        got = await api.call(
            "kroger_restock", "lookup_product", {"upc": "049000028911"}
        )
        assert got["found"] is True
        assert got["upc"] == "0004900002891" and got["barcode"] == "049000028911"
    finally:
        await api.close()


async def test_real_barcode_picture_uses_upc_key(
    rig: Rig, grocy: FakeGrocy, ha: FakeHomeAssistant, host: ImageHost
) -> None:
    coke = grocy.add_product("Coke", ["049000028911"])
    k = FakeKroger(ha)
    k.add("049000028911", name="Coca-Cola", image_url=host.url("ok.png"))
    await start(rig, ha)
    await rig.h.relink(EPC, barcode="049000028911")
    assert rig.h.pictures is not None
    await rig.h.pictures.wait_idle()
    assert k.lookups() == [{"upc": "049000028911"}]  # passed as scanned
    assert coke.picture_file_name == f"pantryhub_{coke.id}.jpg"


@pytest.mark.parametrize("reason", ["not_found", "plu_code", "not_a_product_barcode"])
async def test_not_a_kroger_product_skipped_30_days(
    rig: Rig, grocy: FakeGrocy, ha: FakeHomeAssistant, reason: str
) -> None:
    beans = grocy.add_product("Bananas", ["4011"])
    k = FakeKroger(ha)
    k.reasons["0000000004011"] = reason
    await start(rig, ha)
    await rig.h.relink(EPC, barcode="4011")
    assert rig.h.pictures is not None and rig.store is not None
    await rig.h.pictures.wait_idle()
    assert await rig.store.picture_miss(beans.id) == rig.clock()
    assert len(k.lookups()) == 1  # not retried


async def test_rate_limited_backs_off_an_hour(
    tmp_path: Path, grocy: FakeGrocy, ha: FakeHomeAssistant, host: ImageHost
) -> None:
    from pantry_hub.grocy import GrocyClient
    from pantry_hub.pictures import RATE_LIMIT_BACKOFF_S

    from .fake_grocy import API_KEY

    clock = FakeClock()
    store = await Store.open(tmp_path / "db")
    client = GrocyClient(grocy.url, API_KEY)
    api = HomeAssistantApi(ha.rest_url, TOKEN)
    a = grocy.add_product("A", ["1"])
    b = grocy.add_product("B", ["2"])
    k = FakeKroger(ha)
    k.add("1", image_url=host.url("ok.png"))
    k.add("2", image_url=host.url("ok.png"))
    k.all_reason = "rate_limited"
    sleeps: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)
        clock.advance(s)
        if s >= RATE_LIMIT_BACKOFF_S - 1:
            k.all_reason = None  # Kroger lifted the limit meanwhile

    svc = PictureService(client, api, store, clock=clock, sleep=fake_sleep)
    try:
        assert svc.start_backfill({a.id: "1", b.id: "2"})
        await svc.wait_idle()
        assert len(k.lookups()) == 1  # stopped at the first rate_limited
        assert svc.progress.text() == (
            "0 of 2. Stopped: Kroger rate limit, try again in an hour"
        )
        assert svc.status_line() == "Kroger rate limit: paused for up to an hour"
        assert await store.picture_miss(a.id) is None  # not a "no picture"
        # Within the hour: no call at all.
        clock.advance(RATE_LIMIT_BACKOFF_S - 60)
        assert await svc.fetch(a.id, "1") == "rate_limited"
        assert len(k.lookups()) == 1
        # After it: lookups again.
        k.all_reason = None
        clock.advance(61)
        assert await svc.fetch(a.id, "1") == "set"
        assert svc.status_line() is None
        # A link-time fetch hitting the limit waits out the hour, then retries.
        k.all_reason = "rate_limited"
        n = len(k.lookups())
        svc.on_link(b.id, "2")
        await svc.wait_idle()
        assert len(k.lookups()) == n + 2  # limited, then the retry an hour later
        assert any(s >= RATE_LIMIT_BACKOFF_S - 1 for s in sleeps)
        assert b.picture_file_name == f"pantryhub_{b.id}.jpg"
    finally:
        await svc.close()
        await api.close()
        await client.close()
        await store.close()


async def test_auth_failed_stops_and_asks_to_sign_in(
    rig: Rig, grocy: FakeGrocy, ha: FakeHomeAssistant, host: ImageHost
) -> None:
    beans = grocy.add_product("Beans", ["0001"])
    rice = grocy.add_product("Rice", ["0002"])
    k = FakeKroger(ha)
    k.add("0001", image_url=host.url("ok.png"))
    k.add("0002", image_url=host.url("ok.png"))
    k.all_reason = "auth_failed"
    await start(rig, ha)
    await rig.h.relink(EPC, barcode="0001")
    assert rig.h.pictures is not None
    await rig.h.pictures.wait_idle()
    await rig.h.relink("E28011700000020F1A2B3C10", barcode="0002")
    await rig.h.pictures.wait_idle()
    assert len(k.lookups()) == 1  # stopped fetching after auth_failed
    async with WebClient(rig.h) as c:
        state = await c.state()
        assert [
            "Product pictures",
            "Kroger Restock needs you to sign in again",
        ] in state["diagnostics"]
        titles = [i["title"] for i in state["health"]["issues"]]
        assert "Kroger Restock needs you to sign in again" in titles
        # Signed in again: Fetch missing pictures tries again.
        k.all_reason = None
        await c.post("/pictures/fetch")
        await eventually(
            lambda: (
                rig.h.pictures is not None
                and rig.h.pictures.progress.finished_at is not None
            )
        )
        state = await c.state()
    assert beans.picture_file_name and rice.picture_file_name
    assert state["pictures"]["auth_failed"] is False
    assert "Kroger Restock needs you to sign in again" not in [
        i["title"] for i in state["health"]["issues"]
    ]
