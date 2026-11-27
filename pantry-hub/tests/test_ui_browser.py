"""Browser smoke tests of the v0.4.0 UI (Playwright + Chromium).

Skipped when Playwright isn't installed (the regular test jobs); the "UI smoke"
CI job installs it (``pip install -e ".[dev,ui]"``) and uploads the
screenshots. Locally: set PANTRY_HUB_SCREENSHOTS=<dir> to keep them.

The page is served through :class:`IngressProxy`, under
``/api/hassio_ingress/<token>/`` like Home Assistant does, so every URL the
page builds is checked against a non-root prefix.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
import os
from pathlib import Path
import time
from typing import Any

from aiochainway.testing import FakeUR4, ScriptedTag
import pytest

from pantry_hub.store import Link
from pantry_hub.web import WebUI

from .conftest import free_port
from .fake_grocy import FakeGrocy
from .fake_ha import FakeHomeAssistant
from .ingress_proxy import IngressProxy
from .test_door_reading import DOOR, DoorRig

pw = pytest.importorskip("playwright.async_api")

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c63f8cfc0f01f0005000201a5f6e3c50000000049454e44ae426082"
)
UNLINKED = "E28011700000020F1A2B5D19"
SIZES = {"phone": (390, 844), "wide": (1280, 800)}


def shots_dir(tmp_path: Path) -> Path:
    d = Path(os.environ.get("PANTRY_HUB_SCREENSHOTS") or tmp_path / "shots")
    d.mkdir(parents=True, exist_ok=True)
    return d


class Stack:
    """Hub + web UI + ingress proxy + browser."""

    def __init__(self, rig: DoorRig) -> None:
        self.rig = rig
        self.ui: WebUI | None = None
        self.proxy: IngressProxy | None = None
        self.pw: Any = None
        self.browser: Any = None
        self.errors: list[str] = []

    async def start(self) -> None:
        self.ui = WebUI(self.rig.h, allowed_peers=["*"])
        port = free_port()
        await self.ui.start("127.0.0.1", port)
        self.proxy = IngressProxy(f"http://127.0.0.1:{port}")
        await self.proxy.start()
        self.pw = await pw.async_playwright().start()
        self.browser = await self.pw.chromium.launch()

    async def stop(self) -> None:
        if self.browser is not None:
            await self.browser.close()
        if self.pw is not None:
            await self.pw.stop()
        if self.proxy is not None:
            await self.proxy.stop()
        if self.ui is not None:
            await self.ui.stop()

    async def page(
        self, size: str = "phone", scheme: str = "light", tab: str = "pantry"
    ) -> Any:
        w, h = SIZES[size]
        page = await self.browser.new_page(
            viewport={"width": w, "height": h}, color_scheme=scheme
        )
        page.on(
            "console",
            lambda m: (
                self.errors.append(f"console {m.type}: {m.text}")
                if m.type in ("error", "warning")
                else None
            ),
        )
        page.on("pageerror", lambda e: self.errors.append(f"pageerror: {e}"))
        assert self.proxy is not None
        prefix = self.proxy.prefix
        page.on(
            "request",
            lambda r: (
                self.errors.append(f"request outside ingress: {r.url}")
                if prefix not in r.url
                else None
            ),
        )
        await page.goto(self.proxy.url + f"#/{tab}")
        await page.wait_for_selector("#app:not([aria-busy])", timeout=5000)
        return page


async def no_horizontal_scroll(page: Any) -> None:
    width = await page.evaluate(
        "() => [document.documentElement.scrollWidth, window.innerWidth]"
    )
    assert width[0] <= width[1], f"horizontal scroll: {width}"


@pytest.fixture
async def stack(
    tmp_path: Path, grocy: FakeGrocy, ha: FakeHomeAssistant, fake: FakeUR4
) -> AsyncIterator[Stack]:
    ha.add_entity(DOOR, "off", friendly_name="Pantry door", device_class="door")
    ha.add_entity(
        "binary_sensor.back_door", "on", friendly_name="Back door", device_class="door"
    )
    ha.add_entity(
        "binary_sensor.hall_motion",
        "off",
        friendly_name="Hall motion",
        device_class="motion",
    )
    beans = grocy.add_product("Black beans", ["0001"])
    beans.picture = PNG
    oats = grocy.add_product("Rolled oats", ["0002"])
    tomatoes = grocy.add_product("Diced tomatoes", ["0003"])
    epcs = {
        f"E2801170000002{i:010X}": p
        for i, p in enumerate([beans, beans, oats, tomatoes, tomatoes, tomatoes])
    }
    fake.scenario = [ScriptedTag(e) for e in epcs] + [ScriptedTag(UNLINKED)]
    rig = DoorRig(tmp_path, grocy, ha, fake)
    await rig.start(closed_sweep_interval_s=60.0)
    assert rig.store is not None
    for epc, product in epcs.items():
        await rig.store.create_link(
            Link(epc, product.id, product.name, 1.0), location_id=1
        )
    await rig.sweeps(1, timeout=10)
    await rig.settled()
    s = Stack(rig)
    await s.start()
    yield s
    await s.stop()
    await s.rig.stop()  # a test may have swapped in another hub


@pytest.fixture
async def empty_stack(
    tmp_path: Path, grocy: FakeGrocy, ha: FakeHomeAssistant, fake: FakeUR4
) -> AsyncIterator[Stack]:
    """Zero tags, reader offline, no door sensor: what Heather sees today."""
    rig = DoorRig(tmp_path, grocy, ha, fake)
    await rig.start(door_entity="", closed_sweep_interval_s=60.0)
    await fake.stop()
    s = Stack(rig)
    await s.start()
    yield s
    await s.stop()
    await s.rig.stop()  # a test may have swapped in another hub


# Smoke render --------------------------------------------------------------------


@pytest.mark.parametrize("size", ["phone", "wide"])
async def test_smoke_render_every_tab(stack: Stack, size: str, tmp_path: Path) -> None:
    out = shots_dir(tmp_path)
    for scheme in ("light", "dark"):
        page = await stack.page(size, scheme)
        assert await page.text_content("#title") == "Pantry"
        await page.wait_for_selector("text=All good")
        await page.wait_for_selector("text=Black beans")
        assert await page.text_content(".qty >> nth=0") == "2"
        await page.wait_for_selector("text=1 tag isn’t linked to a product")
        await no_horizontal_scroll(page)
        await page.screenshot(
            path=str(out / f"{size}-{scheme}-pantry.png"), full_page=True
        )
        await page.click("nav.tabs >> text=Tags")
        await page.wait_for_selector("text=Not linked yet · 1")
        await page.wait_for_selector("text=Linked · 6")
        await no_horizontal_scroll(page)
        await page.screenshot(
            path=str(out / f"{size}-{scheme}-tags.png"), full_page=True
        )
        await page.click("nav.tabs >> text=Settings")
        await page.wait_for_selector("text=Door: CLOSED")
        await page.wait_for_selector("text=Back door")  # the picker list
        await no_horizontal_scroll(page)
        await page.screenshot(
            path=str(out / f"{size}-{scheme}-settings.png"), full_page=True
        )
        footer = await page.text_content(".foot")
        assert footer is not None and stack.rig.h.version in footer
        assert stack.rig.h.version not in (await page.text_content("#title") or "")
        await page.close()
    assert stack.errors == []


async def test_empty_offline_no_door(empty_stack: Stack, tmp_path: Path) -> None:
    out = shots_dir(tmp_path)
    for size in SIZES:
        page = await empty_stack.page(size)
        await page.wait_for_selector("text=Reader offline")
        await page.wait_for_selector("text=Nothing detected yet")
        await page.wait_for_selector("text=No door sensor")
        await no_horizontal_scroll(page)
        await page.screenshot(
            path=str(out / f"{size}-empty-pantry.png"), full_page=True
        )
        # Details hold the diagnostics.
        await page.click("button.health")
        await page.wait_for_selector("#health-details >> text=Reader online")
        assert await page.get_attribute("button.health", "aria-expanded") == "true"
        await page.click("nav.tabs >> text=Settings")
        await page.wait_for_selector("text=NO SENSOR")
        assert await page.is_disabled("button[data-action=check-start]")
        await page.screenshot(
            path=str(out / f"{size}-empty-settings.png"), full_page=True
        )
        await page.close()
    assert empty_stack.errors == []


# Freshness -----------------------------------------------------------------------


async def test_fresh_data_without_reload(stack: Stack) -> None:
    t0 = time.monotonic()
    page = await stack.page()
    await page.wait_for_selector("text=Black beans")
    assert time.monotonic() - t0 < 3.0  # within 1 s of load in practice
    assert (await page.text_content("#fresh") or "").startswith("Updated")
    # A change on the server shows up by itself (3 s poll), no reload.
    assert stack.rig.store is not None
    await stack.rig.store.add_needs_product("4242", "kitchen", time.time())
    await page.wait_for_selector("text=1 barcode needs a Grocy product", timeout=6000)


async def test_refetch_on_visibility_pageshow_and_focus(stack: Stack) -> None:
    """How the side-menu stale page is fixed: these events refetch at once."""
    page = await stack.page()
    count = "() => window.__pantryHub.poller.count"

    async def after(js: str) -> int:
        before = await page.evaluate(count)
        await page.wait_for_timeout(300)  # past the 250 ms coalescing window
        await page.evaluate(js)
        await page.wait_for_timeout(150)
        return int(await page.evaluate(count)) - int(before)

    visible = """() => {
      Object.defineProperty(document, 'hidden', {value: false, configurable: true});
      document.dispatchEvent(new Event('visibilitychange'));
    }"""
    assert await after(visible) == 1
    assert (
        await after(
            "() => window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true}))"
        )
        == 1
    )
    assert (
        await after(
            "() => window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted: false}))"
        )
        == 0
    )
    assert await after("() => window.dispatchEvent(new Event('focus'))") == 1
    # Hidden: no immediate fetch, and the next one waits 30 s instead of 3 s.
    hidden = """() => {
      Object.defineProperty(document, 'hidden', {value: true, configurable: true});
      document.dispatchEvent(new Event('visibilitychange'));
    }"""
    assert await after(hidden) == 0
    before = await page.evaluate(count)
    await page.wait_for_timeout(4000)
    assert await page.evaluate(count) == before
    assert await after(visible) == 1


async def test_offline_banner_after_three_failures(stack: Stack) -> None:
    page = await stack.page()
    await page.evaluate("() => { window.__pantryHub.poller.visibleMs = 200; }")
    assert stack.ui is not None
    await stack.ui.stop()  # Pantry Hub goes away
    await page.wait_for_selector("text=Can’t reach Pantry Hub — retrying", timeout=8000)
    assert await page.get_attribute("#offline", "role") == "alert"
    stack.errors[:] = [e for e in stack.errors if "Failed to load resource" not in e]


# Actions -------------------------------------------------------------------------


async def test_actions_go_through_fetch(stack: Stack) -> None:
    page = await stack.page(tab="tags")
    reloads: list[str] = []
    page.on("framenavigated", lambda f: reloads.append(f.url))
    # How to link → steps + the manual link form; link the unlinked tag.
    await page.click("button[data-action=toggle-howto]")
    await page.wait_for_selector("text=Double-click the linking station’s button")
    await page.fill(f"form[data-epc='{UNLINKED}'] input[name=target]", "0002")
    await page.click(f"form[data-epc='{UNLINKED}'] button[type=submit]")
    await page.wait_for_selector(f"#toast >> text=Linked {UNLINKED} to Rolled oats")
    await page.wait_for_selector("text=Not linked yet · 0")
    await page.wait_for_selector("text=Linked · 7")
    # Search, then the ⋯ menu → Unlink (with a confirm dialog).
    await page.fill("#tag-search", "rolled")
    await page.wait_for_function(
        "() => document.querySelectorAll('#s-linked li.item').length === 2"
    )
    page.once("dialog", lambda d: asyncio.ensure_future(d.accept()))
    await page.click("#s-linked li.item >> nth=0 >> button[data-action=toggle-menu]")
    await page.click("#s-linked button[data-action=confirm-unlink]")
    await page.wait_for_selector("#toast >> text=Unlinked")
    await page.wait_for_selector("text=Linked · 6")
    # Check now on the Pantry screen.
    await page.click("nav.tabs >> text=Pantry")
    await page.click("text=Check now")
    await page.wait_for_selector("#toast >> text=Checking the pantry now")
    assert all("#/" in url for url in reloads), reloads  # hash changes only
    assert stack.errors == []


# Settings: picker and door check -----------------------------------------------


async def test_picker_search_and_select(stack: Stack) -> None:
    await stack.rig.stop()
    rig = DoorRig(stack.rig.tmp, stack.rig.grocy, stack.rig.ha, stack.rig.fake)
    await rig.start(door_entity="", closed_sweep_interval_s=60.0)
    stack.rig = rig
    assert stack.ui is not None
    stack.ui.hub = rig.h
    page = await stack.page(tab="settings")
    await page.wait_for_selector("text=No sensor chosen yet")
    await page.fill("#picker-search", "back")
    await page.wait_for_function(
        "() => document.querySelectorAll('#s-picker .option').length === 1"
    )
    await page.click("#s-picker .option")
    await page.wait_for_selector("#toast >> text=Door sensor: binary_sensor.back_door")
    await page.wait_for_selector("text=Door: OPEN")  # Back door is "on"
    assert rig.h.door_entity_source == "web_ui"
    assert stack.errors == []


async def test_door_check_looks_right(stack: Stack) -> None:
    page = await stack.page(tab="settings")
    await page.wait_for_selector("text=Door: CLOSED")
    await page.click("button[data-action=check-start]")
    await page.wait_for_selector("text=Open the pantry door now…")
    await stack.rig.ha.set_state(DOOR, "on")
    await page.wait_for_selector("text=Looks right ✓", timeout=8000)
    assert stack.rig.h.door_inverted is False


async def test_door_check_offers_flip_for_reversed_sensor(stack: Stack) -> None:
    # A reversed sensor: the door is shut but it says "on".
    await stack.rig.ha.set_state(DOOR, "on")
    page = await stack.page(tab="settings")
    await page.wait_for_selector("text=Door: OPEN")
    await page.click("button[data-action=check-start]")
    await page.wait_for_selector("text=Open the pantry door now…")
    await stack.rig.ha.set_state(DOOR, "off")  # opened: it says "off"
    await page.wait_for_selector(
        "text=This sensor reports the opposite. Flip it?", timeout=8000
    )
    await page.click("button[data-action=check-flip]")
    await page.wait_for_selector("text=Flipped ✓")
    await page.wait_for_selector("text=Door: OPEN")
    assert stack.rig.h.door_inverted is True
    assert stack.rig.store is not None
    assert await stack.rig.store.get_meta("door_inverted") == "1"
    # The Advanced switch shows it and turns it back.
    await page.click("summary >> text=Advanced")
    assert await page.is_checked("#inverted")
    await page.click("#inverted")
    await page.wait_for_selector("#toast >> text=Open/closed as reported")
    await page.wait_for_selector("text=Door: CLOSED")
    assert await stack.rig.store.get_meta("door_inverted") == "0"
    assert stack.errors == []
