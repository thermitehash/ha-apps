from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from aiohttp import web
import pytest

from pantry_hub.config import (
    ConfigError,
    load_options,
    mqtt_from_env,
    mqtt_from_supervisor,
    parse_options,
)
from pantry_hub.homeassistant import door_from_state
from pantry_hub.presence import Door

GOOD: dict[str, Any] = {
    "reader_host": "192.168.99.202",
    "grocy_url": "http://a0d7b954-grocy/",
    "grocy_api_key": "abc",
    "grocy_location_id": 2,
    "door_entity": "binary_sensor.pantry_door",
}


def test_defaults_and_derived() -> None:
    o = parse_options(GOOD, env={})
    assert o.grocy_url == "http://a0d7b954-grocy"
    assert o.reader_source_ip == "192.168.99.202"
    assert o.arrive_reads == 3 and o.arrive_window_s == 10 and o.depart_after_s == 900
    assert o.max_reader_power_dbm == 27
    assert o.grocy_browser_url == "http://a0d7b954-grocy"
    assert "abc" not in repr(o)  # API key never logged


def test_env_ports_and_overrides() -> None:
    o = parse_options(
        {
            **GOOD,
            "allowed_reader_ip": "10.0.0.5",
            "grocy_ui_url": "https://x/",
            "reader_host": "ur4.lan",
        },
        env={"PANTRY_HUB_UDP_PORT": "9999", "PANTRY_HUB_WEB_PORT": "8100"},
    )
    assert o.udp_port == 9999 and o.web_port == 8100
    assert o.reader_source_ip == "10.0.0.5"
    assert o.grocy_browser_url == "https://x"
    assert (
        parse_options({**GOOD, "reader_host": "ur4.lan"}, env={}).reader_source_ip
        is None
    )


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"reader_host": ""}, "reader_host is required"),
        ({"grocy_url": "a0d7b954-grocy"}, "grocy_url must be"),
        ({"grocy_url": "ftp://x"}, "grocy_url must be"),
        ({"grocy_api_key": ""}, "grocy_api_key is required"),
        ({"allowed_reader_ip": "nope"}, "allowed_reader_ip"),
        ({"grocy_ui_url": "x"}, "grocy_ui_url"),
        ({"arrive_reads": 0}, "arrive_reads"),
        ({"arrive_reads": True}, "arrive_reads"),
        ({"arrive_reads": 2.5}, "arrive_reads"),
        ({"arrive_window_s": "10"}, "arrive_window_s"),
        ({"depart_after_s": 30, "arrive_window_s": 60}, "must be longer"),
        (
            {
                "depart_requires_door_closed": True,
                "reading_mode": "continuous",
                "door_entity": "",
            },
            "door_entity is empty",
        ),
        ({"depart_requires_door_closed": "yes"}, "true or false"),
        ({"door_entity_topic": "a/b"}, "replaced by door_entity"),
        ({"reading_mode": "door", "door_entity": ""}, "door_entity is required"),
        ({"door_entity": "sensor.door"}, "binary_sensor"),
        ({"reading_mode": "trigger"}, "reading_mode"),
        ({"sweep_s": 400}, "sweep_s"),
        ({"depart_after_missed_sweeps": 0}, "depart_after_missed_sweeps"),
        ({"max_reader_power_dbm": 30}, "FCC"),
        ({"log_level": "loud"}, "log_level"),
        ({"reader_host": 5}, "must be text"),
    ],
)
def test_validation_errors(change: dict[str, Any], message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        parse_options({**GOOD, **change}, env={})


def test_none_string_is_default() -> None:
    assert parse_options({**GOOD, "grocy_ui_url": None}, env={}).grocy_ui_url == ""


def test_door_mode_defaults() -> None:
    o = parse_options(GOOD, env={})
    assert o.reading_mode == "door" and o.door_entity == "binary_sensor.pantry_door"
    assert (o.open_continuous_s, o.open_sweep_interval_s, o.sweep_s) == (600, 300, 45)
    assert (o.closed_sweep_interval_s, o.depart_after_missed_sweeps) == (3600, 2)
    assert o.reader_tcp_port == 8888
    c = parse_options({**GOOD, "reading_mode": "continuous", "door_entity": ""}, env={})
    assert c.reading_mode == "continuous"


def test_load_options(tmp_path: Path) -> None:
    f = tmp_path / "options.json"
    with pytest.raises(ConfigError, match="not found"):
        load_options(f)
    f.write_text("{bad")
    with pytest.raises(ConfigError, match="not valid JSON"):
        load_options(f)
    f.write_text("[]")
    with pytest.raises(ConfigError, match="JSON object"):
        load_options(f)
    f.write_text(json.dumps(GOOD))
    assert load_options(f).grocy_location_id == 2


def test_mqtt_from_env() -> None:
    assert mqtt_from_env({}) is None
    s = mqtt_from_env(
        {
            "PANTRY_HUB_MQTT_HOST": "h",
            "PANTRY_HUB_MQTT_PORT": "1884",
            "PANTRY_HUB_MQTT_USERNAME": "u",
        }
    )
    assert s is not None and (s.host, s.port, s.username, s.password) == (
        "h",
        1884,
        "u",
        None,
    )


async def _supervisor(reply: tuple[int, Any]) -> tuple[web.AppRunner, str]:
    async def handler(request: web.Request) -> web.Response:
        assert request.headers["Authorization"] == "Bearer tok"
        return web.json_response(reply[1], status=reply[0])

    app = web.Application()
    app.router.add_get("/services/mqtt", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    return runner, f"http://127.0.0.1:{port}/services/mqtt"


async def test_mqtt_from_supervisor() -> None:
    runner, url = await _supervisor(
        (
            200,
            {
                "result": "ok",
                "data": {
                    "host": "core-mosquitto",
                    "port": 1883,
                    "username": "addons",
                    "password": "pw",
                    "ssl": False,
                },
            },
        )
    )
    try:
        s = await mqtt_from_supervisor("tok", url)
        assert (s.host, s.username, s.password, s.ssl) == (
            "core-mosquitto",
            "addons",
            "pw",
            False,
        )
        assert "pw" not in repr(s)
    finally:
        await runner.cleanup()
    for reply in [(400, {"result": "error"}), (200, {"data": {}})]:
        runner, url = await _supervisor(reply)
        try:
            with pytest.raises(ConfigError):
                await mqtt_from_supervisor("tok", url)
        finally:
            await runner.cleanup()
    with pytest.raises(ConfigError, match="cannot reach"):
        await mqtt_from_supervisor("tok", "http://127.0.0.1:9/services/mqtt", timeout=1)


@pytest.mark.parametrize(
    ("state", "door"),
    [
        ("on", Door.OPEN),
        ("off", Door.CLOSED),
        ("unavailable", Door.UNKNOWN),
        ("unknown", Door.UNKNOWN),
        (None, Door.UNKNOWN),
        ("open", Door.UNKNOWN),
    ],
)
def test_door_from_state(state: object, door: Door) -> None:
    assert door_from_state(state) is door
