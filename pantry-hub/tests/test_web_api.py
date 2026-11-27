"""v0.4.0: cache headers and ``GET /api/state`` (the page polls it)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from aiochainway.testing import FakeUR4, ScriptedTag
import aiohttp
import pytest

from pantry_hub.store import Link
from pantry_hub.web import WebUI
from pantry_hub.web.state import health_for, issues_for

from .conftest import FakeClock, eventually, free_port
from .fake_grocy import FakeGrocy
from .fake_ha import FakeHomeAssistant
from .rig import Rig
from .test_door_reading import DOOR, DoorRig

PREFIX = "/api/hassio_ingress/abc123"
HDR = {"X-Ingress-Path": PREFIX}


async def serve(hub: Any) -> tuple[WebUI, str]:
    ui = WebUI(hub, allowed_peers=["*"])
    port = free_port()
    await ui.start("127.0.0.1", port)
    return ui, f"http://127.0.0.1:{port}"


@pytest.fixture
async def rig(tmp_path: Path, grocy: FakeGrocy, clock: FakeClock) -> AsyncIterator[Rig]:
    r = Rig(tmp_path, grocy, None, clock)
    await r.start()
    yield r
    await r.stop()
    r.close()


# Headers -------------------------------------------------------------------------


async def test_html_and_json_are_no_store(rig: Rig) -> None:
    ui, base = await serve(rig.h)
    try:
        async with aiohttp.ClientSession() as s:
            for path in ("/", "/api/state"):
                async with s.get(base + path, headers=HDR) as r:
                    assert r.status == 200, path
                    assert r.headers["Cache-Control"] == "no-store", path
                    assert r.headers["Pragma"] == "no-cache", path
            async with s.get(base + "/no-such-page", headers=HDR) as r:
                assert r.status == 404
                assert r.headers["Cache-Control"] == "no-store"
    finally:
        await ui.stop()


async def test_refused_peer_is_no_store(rig: Rig) -> None:
    ui = WebUI(rig.h)  # only the ingress proxy may connect
    port = free_port()
    await ui.start("127.0.0.1", port)
    try:
        async with (
            aiohttp.ClientSession() as s,
            s.get(f"http://127.0.0.1:{port}/api/state") as r,
        ):
            assert r.status == 403
    finally:
        await ui.stop()


# /api/state ----------------------------------------------------------------------

TOP_KEYS = {
    "version",
    "now",
    "health",
    "reader",
    "grocy",
    "mqtt",
    "door",
    "reading",
    "pantry",
    "unlinked",
    "links",
    "failed_ops",
    "needs_product",
    "events",
    "options",
    "diagnostics",
}


async def test_state_shape_empty_pantry_reader_offline(rig: Rig) -> None:
    ui, base = await serve(rig.h)
    try:
        async with (
            aiohttp.ClientSession() as s,
            s.get(base + "/api/state", headers=HDR) as r,
        ):
            assert r.content_type == "application/json"
            data = await r.json()
    finally:
        await ui.stop()
    assert set(data) == TOP_KEYS
    assert data["version"] == rig.h.version
    assert data["pantry"] == {
        "items": [],
        "products": 0,
        "linked_present": 0,
        "present_total": 0,
        "unlinked_present": 0,
    }
    assert data["unlinked"] == [] and data["links"] == [] and data["events"] == []
    assert data["reader"]["online"] is False  # continuous mode, no datagrams
    assert data["health"]["level"] == "problem"
    assert data["health"]["title"] == "Reader offline"
    assert "127.0.0.1" in data["health"]["detail"]
    assert data["mqtt"] == {"enabled": False, "connected": None}
    assert all(len(row) == 2 for row in data["diagnostics"])
    assert {"reader_antennas", "reader_power_dbm", "reader_connection"} <= set(
        data["options"]
    )


async def test_state_groups_present_items_by_product(
    rig: Rig, grocy: FakeGrocy
) -> None:
    beans = grocy.add_product("Black beans", ["0001"])
    oats = grocy.add_product("Rolled oats", ["0002"])
    assert rig.store is not None
    for epc, product in (("AAA1", beans), ("AAA2", beans), ("AAA3", oats)):
        await rig.store.create_link(
            Link(epc, product.id, product.name, rig.clock()), location_id=1
        )
    await rig.arrive("AAA1", "AAA2", "AAA3", "BBB9")  # BBB9 is unlinked
    await rig.idle()
    ui, base = await serve(rig.h)
    try:
        async with aiohttp.ClientSession() as s, s.get(base + "/api/state") as r:
            data = await r.json()
    finally:
        await ui.stop()
    items = data["pantry"]["items"]
    assert [(i["name"], i["quantity"]) for i in items] == [
        ("Black beans", 2),
        ("Rolled oats", 1),
    ]
    assert sorted(items[0]["epcs"]) == ["AAA1", "AAA2"]
    assert items[0]["since"] is not None
    assert data["pantry"]["present_total"] == 4
    assert data["pantry"]["unlinked_present"] == 1
    assert [u["epc"] for u in data["unlinked"]] == ["BBB9"]
    assert data["unlinked"][0]["present"] is True
    assert {lk["epc"] for lk in data["links"]} == {"AAA1", "AAA2", "AAA3"}
    kinds = [(e["kind"], e["epc"]) for e in data["events"]]
    assert ("arrived", "AAA1") in kinds and ("arrived", "BBB9") in kinds
    assert data["events"][0]["at"] >= data["events"][-1]["at"]  # newest first


async def test_state_door_mode_needs_door_sensor(
    tmp_path: Path, grocy: FakeGrocy, ha: FakeHomeAssistant, fake: FakeUR4
) -> None:
    fake.scenario = [ScriptedTag("E28011700000020F1A2B3E01", present_from=1e9)]
    rig = DoorRig(tmp_path, grocy, ha, fake)
    try:
        await rig.start(door_entity="", closed_sweep_interval_s=60.0)
        await rig.sweeps(1)
        ui, base = await serve(rig.h)
        try:
            async with aiohttp.ClientSession() as s, s.get(base + "/api/state") as r:
                data = await r.json()
        finally:
            await ui.stop()
    finally:
        await rig.stop()
    assert data["door"]["needed"] is True and data["door"]["entity_id"] is None
    assert data["reader"]["online"] is True
    assert data["reader"]["antennas_enabled"] == [1]
    assert data["reading"]["last_completed_sweep_at"] is not None
    assert data["reading"]["next_sweep_at"] > data["now"]  # an hour-ish away
    assert data["health"]["title"] == "No door sensor chosen"
    assert data["health"]["action"] == "settings"


async def test_state_door_state_and_raw(
    tmp_path: Path, grocy: FakeGrocy, ha: FakeHomeAssistant, fake: FakeUR4
) -> None:
    await ha.set_state(DOOR, "on")
    rig = DoorRig(tmp_path, grocy, ha, fake)
    try:
        await rig.start()
        await eventually(lambda: rig.h.door_status()["raw"] == "on")
        ui, base = await serve(rig.h)
        try:
            async with aiohttp.ClientSession() as s, s.get(base + "/api/state") as r:
                data = await r.json()
        finally:
            await ui.stop()
    finally:
        await rig.stop()
    assert (data["door"]["state"], data["door"]["raw"]) == ("open", "on")
    assert data["door"]["source"] == "options" and data["door"]["inverted"] is False


# Health priority -----------------------------------------------------------------


def _state(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "reader": {
            "online": True,
            "host": "192.168.99.202",
            "config_error": None,
            "dead_ports": [],
        },
        "grocy": {"online": True, "failed": 0, "last_error": None},
        "mqtt": {"enabled": True, "connected": True},
        "door": {"needed": False, "missing": False, "entity_id": DOOR},
    }
    for key, value in over.items():
        base[key] = {**base[key], **value}
    return base


def test_health_all_good() -> None:
    h = health_for(_state())
    assert (h["level"], h["title"], h["issues"]) == ("ok", "All good", [])


def test_health_priority_order() -> None:
    state = _state(
        reader={"online": False, "dead_ports": [2]},
        grocy={"failed": 3},
        mqtt={"connected": False},
        door={"needed": True},
    )
    titles = [i.title for i in issues_for(state)]
    assert titles == [
        "Reader offline",
        "3 Grocy updates failed",
        "MQTT disconnected",
        "No door sensor chosen",
        "Antenna on port 2 gave no reads",
    ]
    h = health_for(state)
    assert h["title"] == "Reader offline"
    assert h["detail"] == "Check it’s powered and on the network (192.168.99.202)."
    assert len(h["issues"]) == 5


def test_health_specific_messages() -> None:
    assert health_for(_state(reader={"config_error": "antennas read back as [1, 2]"}))[
        "title"
    ] == ("Reader antenna settings could not be applied")
    assert health_for(_state(reader={"online": None}))["title"] == (
        "Connecting to the reader…"
    )
    h = health_for(_state(grocy={"failed": 1}))
    assert (h["title"], h["action"], h["action_label"]) == (
        "1 Grocy update failed",
        "tags",
        "Retry",
    )
    assert health_for(_state(grocy={"online": False, "last_error": "timeout"}))[
        "detail"
    ].startswith("timeout.")
    assert health_for(_state(mqtt={"enabled": False, "connected": None}))["level"] == (
        "ok"
    )
    assert health_for(_state(door={"missing": True}))["title"] == (
        "Door sensor not found"
    )
