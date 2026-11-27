"""v0.3.2: door sensor picker, option precedence, needs-door-sensor state."""

from __future__ import annotations

import asyncio
from pathlib import Path
import re
from typing import Any

from aiochainway.testing import FakeUR4
import aiohttp
import pytest

from pantry_hub.__main__ import ingress_settings_url, resolve_home_assistant, run
from pantry_hub.config import DOOR_ENTITY_RE, ConfigError, parse_options
from pantry_hub.homeassistant import (
    NEEDED_NOTIFICATION_ID,
    door_candidates,
    fetch_states,
)
from pantry_hub.reading import Activity

from .conftest import BrokerHandle, eventually, free_port, make_options
from .fake_grocy import FakeGrocy
from .fake_ha import TOKEN, FakeHomeAssistant
from .test_config import GOOD
from .test_door_reading import DOOR, DoorRig
from .test_main import env_for
from .web_client import WebClient

APP = Path(__file__).resolve().parents[1]
OTHER_DOOR = "binary_sensor.back_door"


def _state(entity: str, state: str = "off", **attrs: Any) -> dict[str, Any]:
    return {"entity_id": entity, "state": state, "attributes": attrs}


# Picker list and sorting ---------------------------------------------------------


def test_door_candidates_sorting() -> None:
    states = [
        _state(
            "binary_sensor.motion",
            "on",
            friendly_name="Hall motion",
            device_class="motion",
        ),
        _state(
            "binary_sensor.bedroom_window",
            friendly_name="Bedroom window",
            device_class="window",
        ),
        _state("sensor.temperature", "21", friendly_name="Temperature"),
        _state(
            "binary_sensor.pantry_door",
            friendly_name="pantry door",
            device_class="door",
        ),
        _state(
            "binary_sensor.garage",
            "on",
            friendly_name="Garage",
            device_class="garage_door",
        ),
        _state(
            "binary_sensor.front_door", friendly_name="Front door", device_class="door"
        ),
        _state("binary_sensor.no_name", "unavailable"),
        _state(
            "binary_sensor.fridge",
            friendly_name="Fridge contact",
            device_class="opening",
        ),
        _state("binary_sensor.alarm", friendly_name="Alarm armed"),
        {"entity_id": 5, "state": "on"},  # junk is skipped
    ]
    found = door_candidates(states)
    assert [c.entity_id for c in found] == [
        "binary_sensor.front_door",  # door (by name, case-insensitive)
        "binary_sensor.pantry_door",
        "binary_sensor.garage",  # garage_door
        "binary_sensor.fridge",  # opening
        "binary_sensor.bedroom_window",  # window
        "binary_sensor.alarm",  # the rest, by name ("Alarm armed")
        "binary_sensor.no_name",  # no friendly name: sorts by entity id
        "binary_sensor.motion",  # "Hall motion"
    ]
    assert [c.door_like for c in found] == [True] * 5 + [False] * 3
    no_name = found[-2]
    assert no_name.name == "binary_sensor.no_name" and no_name.state == "unavailable"
    assert no_name.device_class is None
    assert found[1].name == "pantry door" and found[1].state == "off"


async def test_fetch_states_from_home_assistant(ha: FakeHomeAssistant) -> None:
    ha.add_entity(DOOR, "off", friendly_name="Pantry door", device_class="door")
    ha.add_entity("light.kitchen", "on")
    states = await fetch_states(TOKEN, url=ha.url)
    assert {s["entity_id"] for s in states} == {DOOR, "light.kitchen"}
    assert [c.entity_id for c in door_candidates(states)] == [DOOR]
    ha.fail_get_states = True
    with pytest.raises(ValueError, match="get_states failed"):
        await fetch_states(TOKEN, url=ha.url)
    with pytest.raises(ValueError, match="refused the token"):
        await fetch_states("wrong", url=ha.url)


# Schema regex --------------------------------------------------------------------


def _schema_regex() -> str:
    text = (APP / "config.yaml").read_text()
    schema = text[text.index("\nschema:") :]
    m = re.search(r"^  door_entity: match\((.*)\)$", schema, re.MULTILINE)
    assert m, "door_entity schema is not match(...)"
    return m.group(1)


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("", True),
        ("binary_sensor.pantry_door", True),
        ("binary_sensor.door_2", True),
        ("sensor.pantry_door", False),
        ("binary_sensor.Pantry", False),
        ("binary_sensor.", False),
        ("binary_sensor.pantry door", False),
        ("binary_sensor_pantry", False),
        ("binary_sensor.pantry_door ", False),
        ("binary_sensor.a.b", False),
    ],
)
def test_schema_regex_matches_runtime_validation(value: str, ok: bool) -> None:
    """The Supervisor rejects typos at save time with the same rule the app uses."""
    assert bool(re.match(_schema_regex(), value)) is ok
    assert bool(value == "" or DOOR_ENTITY_RE.fullmatch(value)) is ok
    if value != value.strip():
        return  # the app strips surrounding spaces; the Supervisor refuses them
    if ok:
        parse_options({**GOOD, "door_entity": value}, env={})
    else:
        with pytest.raises(ConfigError):
            parse_options({**GOOD, "door_entity": value}, env={})


def test_door_mode_without_door_entity_is_valid() -> None:
    o = parse_options({**GOOD, "reading_mode": "door", "door_entity": ""}, env={})
    assert o.reading_mode == "door" and o.door_entity == ""
    c = parse_options(
        {
            **GOOD,
            "reading_mode": "continuous",
            "door_entity": "",
            "depart_requires_door_closed": True,
        },
        env={},
    )
    assert c.depart_requires_door_closed and not c.door_entity


# Environment ---------------------------------------------------------------------


def test_resolve_home_assistant_and_settings_url() -> None:
    assert resolve_home_assistant({}) == (None, None)
    assert resolve_home_assistant({"SUPERVISOR_TOKEN": "t"}) == (
        "ws://supervisor/core/websocket",
        "t",
    )
    assert resolve_home_assistant(
        {"PANTRY_HUB_HA_TOKEN": "d", "PANTRY_HUB_HA_URL": "ws://x/api/websocket"}
    ) == ("ws://x/api/websocket", "d")
    assert ingress_settings_url({"HOSTNAME": "1a2b3c4d-pantry-hub"}) == (
        "/hassio/ingress/1a2b3c4d_pantry_hub"
    )
    assert ingress_settings_url({"HOSTNAME": "local-pantry-hub"}) == (
        "/hassio/ingress/local_pantry_hub"
    )
    assert ingress_settings_url({"HOSTNAME": "my-laptop"}) is None
    assert ingress_settings_url({}) is None


# Precedence ----------------------------------------------------------------------


@pytest.fixture
async def rig(
    tmp_path: Path, grocy: FakeGrocy, ha: FakeHomeAssistant, fake: FakeUR4
) -> Any:
    r = DoorRig(tmp_path, grocy, ha, fake)
    yield r
    await r.stop()


async def test_options_win_over_web_choice(rig: DoorRig, ha: FakeHomeAssistant) -> None:
    await ha.set_state(DOOR, "off")
    await ha.set_state(OTHER_DOOR, "on")
    await rig.start()  # door_entity = DOOR in the app options
    assert rig.h.door_entity == DOOR and rig.h.door_entity_source == "options"
    msg = await rig.h.set_door_entity(OTHER_DOOR)
    assert "app options" in msg and DOOR in msg
    assert rig.store is not None
    assert await rig.store.get_meta("door_entity") == OTHER_DOOR  # saved anyway
    assert rig.h.door_entity == DOOR and rig.h.door_entity_source == "options"
    assert rig.h.door_source is not None and rig.h.door_source.entity_id == DOOR
    await asyncio.sleep(0.3)
    assert rig.h.activity is not Activity.CONTINUOUS  # OTHER_DOOR (open) ignored
    reading = rig.h.state()["reading"]
    assert reading["door_entity"] == DOOR
    assert reading["door_entity_source"] == "options"
    assert reading["door_sensor_needed"] is False


async def test_web_choice_used_when_options_empty(
    rig: DoorRig, ha: FakeHomeAssistant
) -> None:
    await ha.set_state(OTHER_DOOR, "off")
    await rig.start(door_entity="")
    assert rig.h.state()["reading"]["door_sensor_needed"] is True
    assert rig.h.state()["reading"]["door_entity"] is None
    assert await rig.h.set_door_entity(OTHER_DOOR) == f"Door sensor: {OTHER_DOOR}"
    assert rig.h.door_entity == OTHER_DOOR
    assert rig.h.door_entity_source == "web_ui"
    await ha.set_state(OTHER_DOOR, "on")
    await eventually(lambda: rig.h.activity is Activity.CONTINUOUS)
    with pytest.raises(ValueError, match="not a binary_sensor"):
        await rig.h.set_door_entity("sensor.door")
    assert rig.h.door_entity == OTHER_DOOR


async def test_web_choice_survives_restart(
    tmp_path: Path, grocy: FakeGrocy, ha: FakeHomeAssistant, fake: FakeUR4
) -> None:
    await ha.set_state(OTHER_DOOR, "on")
    first = DoorRig(tmp_path, grocy, ha, fake)
    try:
        await first.start(door_entity="")
        await first.h.set_door_entity(OTHER_DOOR)
    finally:
        await first.stop()
    second = DoorRig(tmp_path, grocy, ha, fake)
    try:
        await second.start(door_entity="")
        assert second.h.door_entity == OTHER_DOOR
        assert second.h.door_entity_source == "web_ui"
        await eventually(lambda: second.h.activity is Activity.CONTINUOUS)
    finally:
        await second.stop()


# Needs door sensor ---------------------------------------------------------------


async def test_needs_door_sensor_state(rig: DoorRig, ha: FakeHomeAssistant) -> None:
    await ha.set_state(OTHER_DOOR, "off")
    await rig.start(
        door_entity="",
        closed_sweep_interval_s=1.0,
        settings_url="/hassio/ingress/local_pantry_hub",
    )
    h = rig.h
    assert h.state()["reading"]["door_entity_source"] is None
    await eventually(lambda: ha.calls("persistent_notification", "create") != [])
    note = ha.calls("persistent_notification", "create")[0]
    assert note["notification_id"] == NEEDED_NOTIFICATION_ID
    assert "/hassio/ingress/local_pantry_hub" in note["message"]
    await rig.sweeps(2, timeout=6)  # startup sweep + closed schedule
    assert h.activity is not Activity.CONTINUOUS
    assert h.state()["reading"]["door_sensor_needed"] is True
    # Choosing a sensor dismisses the notification and follows the door.
    await h.set_door_entity(OTHER_DOOR)
    await eventually(
        lambda: (
            {"notification_id": NEEDED_NOTIFICATION_ID}
            in ha.calls("persistent_notification", "dismiss")
        )
    )
    assert h.state()["reading"]["door_sensor_needed"] is False
    await ha.set_state(OTHER_DOOR, "on")
    await eventually(lambda: h.activity is Activity.CONTINUOUS)
    # Clearing it goes back to the needs-door-sensor state.
    assert await h.set_door_entity("") == "Door sensor cleared"
    assert h.door_sensor_needed
    await eventually(lambda: h.activity is not Activity.CONTINUOUS)
    await eventually(lambda: len(ha.calls("persistent_notification", "create")) >= 2)


async def test_run_starts_without_door_or_reachable_reader(
    tmp_path: Path, grocy: FakeGrocy, broker: BrokerHandle
) -> None:
    """Door mode, no door entity anywhere, reader port closed: still starts."""
    web_port = free_port()
    opts = make_options(
        grocy_url=grocy.url,
        web_port=web_port,
        reading_mode="door",
        door_entity="",
        reader_host="127.0.0.1",
        reader_tcp_port=free_port(),  # nothing listens: unreachable
    )
    stop = asyncio.Event()
    task = asyncio.create_task(run(opts, tmp_path / "data", env_for(broker), stop))
    pages: list[str] = []

    async def page_up() -> bool:
        try:
            async with (
                aiohttp.ClientSession() as s,
                s.get(f"http://127.0.0.1:{web_port}/") as r,
            ):
                pages.append(await r.text())
                return r.status == 200
        except aiohttp.ClientError:
            return False

    await eventually(page_up, timeout=10)
    assert 'id="app"' in pages[-1]  # the shell; data comes from /api/state
    async with (
        aiohttp.ClientSession() as s,
        s.get(f"http://127.0.0.1:{web_port}/api/state") as r,
    ):
        state = await r.json()
    assert state["door"]["needed"] is True
    assert state["health"]["title"] in ("Reader offline", "Connecting to the reader…")
    assert "No door sensor chosen" in [i["title"] for i in state["health"]["issues"]]
    stop.set()
    assert await asyncio.wait_for(task, 20) == 0


# Web UI --------------------------------------------------------------------------


async def test_settings_picker_lists_saves_and_shows_source(
    rig: DoorRig, ha: FakeHomeAssistant
) -> None:
    ha.add_entity(
        "binary_sensor.motion", "on", friendly_name="Hall motion", device_class="motion"
    )
    ha.add_entity(OTHER_DOOR, "off", friendly_name="Back door", device_class="door")
    ha.add_entity(
        "binary_sensor.window",
        "off",
        friendly_name="Window & <b>",
        device_class="window",
    )
    await rig.start(door_entity="")
    async with WebClient(rig.h, prefix="/api/hassio_ingress/abc") as c:
        state = await c.state()
        assert state["door"]["needed"] is True
        assert state["health"]["action"] == "settings" or "No door sensor chosen" in [
            i["title"] for i in state["health"]["issues"]
        ]
        listing = await c.json("/api/door-candidates")
        assert listing["error"] is None
        assert [x["entity_id"] for x in listing["candidates"]] == [
            OTHER_DOOR,
            "binary_sensor.window",
            "binary_sensor.motion",
        ]
        assert listing["candidates"][2] == {
            "entity_id": "binary_sensor.motion",
            "name": "Hall motion",
            "state": "on",
            "device_class": "motion",
            "door_like": False,
        }
        assert (
            listing["candidates"][1]["name"] == "Window & <b>"
        )  # JSON, escaped by the page
        reply = await c.post("/settings/door", door_entity=OTHER_DOOR)
        assert reply == {"ok": True, "message": f"Door sensor: {OTHER_DOOR}"}
        assert rig.store is not None
        assert await rig.store.get_meta("door_entity") == OTHER_DOOR
        door = (await c.state())["door"]
        assert (door["entity_id"], door["source"], door["web_entity_id"]) == (
            OTHER_DOOR,
            "web_ui",
            OTHER_DOOR,
        )
        # A bad value is refused with a message, nothing saved.
        bad = await c.post("/settings/door", door_entity="sensor.x")
        assert bad["ok"] is False and "not a binary_sensor" in bad["message"]
        assert await rig.store.get_meta("door_entity") == OTHER_DOOR
        # HA listing fails: an error, and the saved choice is still reported.
        ha.fail_get_states = True
        listing = await c.json("/api/door-candidates")
        assert listing["candidates"] == [] and "get_states failed" in listing["error"]
        assert (await c.state())["door"]["web_entity_id"] == OTHER_DOOR


async def test_settings_shows_options_source(
    rig: DoorRig, ha: FakeHomeAssistant
) -> None:
    await ha.set_state(DOOR, "off")
    await rig.start()
    async with WebClient(rig.h) as c:
        door = (await c.state())["door"]
    assert (door["entity_id"], door["source"]) == (DOOR, "options")
    assert door["needed"] is False


async def test_settings_without_home_assistant_token(
    tmp_path: Path, grocy: FakeGrocy, fake: FakeUR4, ha: FakeHomeAssistant
) -> None:
    rig = DoorRig(tmp_path, grocy, ha, fake)
    try:
        await rig.start(door_entity="")
        rig.h.ha_token = None  # as when not running under the Supervisor
        with pytest.raises(ValueError, match="not available"):
            await rig.h.door_candidates()
    finally:
        await rig.stop()


# v0.3.2 decisions: inverted sensor, live state, not-found notification ------------


async def test_inverted_sensor_and_live_state(
    tmp_path: Path, grocy: FakeGrocy, ha: FakeHomeAssistant, fake: FakeUR4
) -> None:
    await ha.set_state(DOOR, "on")  # raw on, but this sensor means closed
    rig = DoorRig(tmp_path, grocy, ha, fake)
    try:
        await rig.start()
        await eventually(lambda: rig.h.activity is Activity.CONTINUOUS)
        await rig.h.set_door_inverted(True)
        status = rig.h.door_status()
        assert status["door"] == "closed" and status["raw"] == "on"
        assert status["inverted"] is True
        assert (status["door"], status["raw"], status["inverted"]) == (
            "closed",
            "on",
            True,
        )
        await eventually(lambda: rig.h.activity is not Activity.CONTINUOUS)
        await ha.set_state(DOOR, "off")  # raw off → open when inverted
        await eventually(lambda: rig.h.activity is Activity.CONTINUOUS)
        st = rig.h.door_status()
        assert (st["door"], st["raw"], st["inverted"]) == ("open", "off", True)
        reading = rig.h.state()["reading"]
        assert reading["door_inverted"] is True and reading["door_raw"] == "off"
        await ha.set_state(DOOR, "unavailable")  # unknown stays unknown
        await eventually(lambda: rig.h.door_status()["door"] == "unknown")
        await ha.set_state(DOOR, "off")
        await eventually(lambda: rig.h.door_status()["door"] == "open")
    finally:
        await rig.stop()
    again = DoorRig(tmp_path, grocy, ha, fake)  # the checkbox survives a restart
    try:
        await again.start()
        assert again.h.door_inverted is True
        await eventually(lambda: again.h.door_status()["door"] == "open")
        await again.h.set_door_inverted(False)
        assert again.h.door_status()["door"] == "closed"
        st = again.h.door_status()
        assert (st["door"], st["raw"], st["inverted"]) == ("closed", "off", False)
    finally:
        await again.stop()


async def test_settings_inverted_switch_and_live_endpoint(
    rig: DoorRig, ha: FakeHomeAssistant
) -> None:
    ha.add_entity(DOOR, "on", friendly_name="Pantry door", device_class="door")
    await rig.start()
    await eventually(lambda: rig.h.door_status()["raw"] == "on")
    async with WebClient(rig.h) as c:
        assert await c.json("/api/door") == {
            "entity_id": DOOR,
            "source": "options",
            "inverted": False,
            "door": "open",
            "raw": "on",
            "missing": False,
            "needed": False,
        }
        # The check step's "Flip" and the Advanced switch both post this.
        reply = await c.post("/settings/inverted", inverted="true")
        assert reply == {"ok": True, "message": "Open/closed reversed"}
        assert rig.store is not None
        assert await rig.store.get_meta("door_inverted") == "1"  # v0.3.4 format
        live = await c.json("/api/door")
        assert live["door"] == "closed" and live["inverted"] is True
        state = await c.state()
        assert state["door"]["inverted"] is True and state["door"]["raw"] == "on"
        await ha.set_state(DOOR, "off")  # the live endpoint follows the door
        await eventually(lambda: rig.h.door_status()["raw"] == "off")
        live = await c.json("/api/door")
        assert (live["door"], live["raw"]) == ("open", "off")
        bad = await c.post("/settings/inverted", inverted="maybe")
        assert bad["ok"] is False
        reply = await c.post("/settings/inverted", inverted="false")
        assert reply["message"] == "Open/closed as reported"
        assert await rig.store.get_meta("door_inverted") == "0"
        # Choosing a sensor and the switch in one request (older form).
        both = await c.post("/settings/door", door_entity="", inverted="on")
        assert both["ok"] is True and rig.h.door_inverted is True


async def test_saved_sensor_not_found_notification(
    rig: DoorRig, ha: FakeHomeAssistant
) -> None:
    await rig.start(door_entity="", settings_url="/hassio/ingress/local_pantry_hub")
    await rig.h.set_door_entity("binary_sensor.gone")  # not in Home Assistant
    await eventually(lambda: rig.h.door_status()["missing"] is True)
    assert rig.h.door_status()["missing"] is True
    await eventually(
        lambda: any(
            c.get("notification_id") == "pantry_hub_door_unavailable"
            for c in ha.calls("persistent_notification", "create")
        )
    )
    note = next(
        c
        for c in ha.calls("persistent_notification", "create")
        if c.get("notification_id") == "pantry_hub_door_unavailable"
    )
    assert note["title"] == "Pantry Hub: door sensor not found"
    assert note["message"].startswith(
        "Saved door sensor `binary_sensor.gone` not found in Home Assistant "
        "(renamed or deleted?)."
    )
    assert "/hassio/ingress/local_pantry_hub" in note["message"]
    # It comes back (e.g. renamed back): found again, notification dismissed.
    await ha.set_state("binary_sensor.gone", "off")
    await rig.h.set_door_entity(None)
    await rig.h.set_door_entity("binary_sensor.gone")
    await eventually(lambda: rig.h.door_status()["raw"] == "off")
    assert rig.h.door_status()["missing"] is False


async def test_removed_sensor_gets_not_found_text_unavailable_gets_generic(
    rig: DoorRig, ha: FakeHomeAssistant
) -> None:
    await ha.set_state(DOOR, "off")
    await rig.start()
    await eventually(lambda: rig.h.door_status()["raw"] == "off")
    await ha.set_state(DOOR, "unavailable")  # exists, but unavailable
    await eventually(lambda: ha.calls("persistent_notification", "create") != [])
    first = ha.calls("persistent_notification", "create")[0]
    assert first["title"] == "Pantry Hub: door sensor unavailable"
    assert "battery" in first["message"]
    await ha.set_state(DOOR, "off")
    await eventually(lambda: ha.calls("persistent_notification", "dismiss") != [])
    await ha.set_state(DOOR, None)  # deleted from Home Assistant
    await eventually(lambda: len(ha.calls("persistent_notification", "create")) >= 2)
    second = ha.calls("persistent_notification", "create")[1]
    assert second["title"] == "Pantry Hub: door sensor not found"
    assert f"Saved door sensor `{DOOR}` not found" in second["message"]
