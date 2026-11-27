from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import signal
from typing import Any

import aiohttp
import pytest

from pantry_hub.__main__ import app_version, check_grocy, main, run, setup_logging
from pantry_hub.config import ConfigError
from pantry_hub.grocy import GrocyClient

from .conftest import BrokerHandle, eventually, free_port, make_options
from .fake_grocy import API_KEY, FakeGrocy


def env_for(broker: BrokerHandle, **extra: str) -> dict[str, str]:
    return {
        "PANTRY_HUB_MQTT_HOST": "127.0.0.1",
        "PANTRY_HUB_MQTT_PORT": str(broker.port),
        "PANTRY_HUB_GROCY_WAIT": "0",
        "PANTRY_HUB_WEB_ALLOW": "127.0.0.1",
        **extra,
    }


async def test_run_starts_serves_and_stops(
    tmp_path: Path, grocy: FakeGrocy, broker: BrokerHandle
) -> None:
    web_port = free_port()
    opts = make_options(grocy_url=grocy.url, udp_port=free_port(), web_port=web_port)
    stop = asyncio.Event()
    task = asyncio.create_task(run(opts, tmp_path / "data", env_for(broker), stop))

    async def page_up() -> bool:
        try:
            async with (
                aiohttp.ClientSession() as s,
                s.get(f"http://127.0.0.1:{web_port}/") as r,
            ):
                return r.status == 200
        except aiohttp.ClientError:
            return False

    await eventually(page_up, timeout=10)
    stop.set()
    assert await asyncio.wait_for(task, 20) == 0
    assert (tmp_path / "data" / "pantry_hub.db").exists()


async def test_sigterm_stops_gracefully(
    tmp_path: Path, grocy: FakeGrocy, broker: BrokerHandle
) -> None:
    opts = make_options(grocy_url=grocy.url, udp_port=free_port(), web_port=free_port())
    task = asyncio.create_task(run(opts, tmp_path, env_for(broker)))
    await eventually(lambda: (tmp_path / "pantry_hub.db").exists(), timeout=10)
    await asyncio.sleep(0.2)
    os.kill(os.getpid(), signal.SIGTERM)
    assert await asyncio.wait_for(task, 20) == 0
    loop = asyncio.get_running_loop()
    loop.remove_signal_handler(signal.SIGTERM)
    loop.remove_signal_handler(signal.SIGINT)


@pytest.mark.parametrize(
    ("change", "fragment"),
    [
        ({"grocy_api_key": "wrong"}, "refused the API key"),
        ({"grocy_location_id": 99}, "does not exist"),
        ({"grocy_url": "http://127.0.0.1:9"}, "not reachable"),
    ],
)
async def test_refuses_to_start(
    tmp_path: Path,
    grocy: FakeGrocy,
    broker: BrokerHandle,
    caplog: pytest.LogCaptureFixture,
    change: dict[str, Any],
    fragment: str,
) -> None:
    opts = make_options(**{"grocy_url": grocy.url, **change})
    with caplog.at_level(logging.ERROR):
        assert await run(opts, tmp_path, env_for(broker)) == 1
    assert fragment in caplog.text
    assert "Refusing to start" in caplog.text


async def test_refuses_without_mqtt(
    tmp_path: Path, grocy: FakeGrocy, caplog: pytest.LogCaptureFixture
) -> None:
    opts = make_options(grocy_url=grocy.url)
    assert await run(opts, tmp_path, {"PANTRY_HUB_GROCY_WAIT": "0"}) == 1
    assert "no MQTT broker" in caplog.text


async def test_check_grocy_waits_for_grocy(grocy: FakeGrocy) -> None:
    grocy.fault("/api/system/info", "500")
    opts = make_options(grocy_url=grocy.url)
    client = GrocyClient(grocy.url, API_KEY)
    try:
        summary = await check_grocy(client, opts, wait=10)
        assert "Grocy 4.7.1 OK" in summary and "'Pantry'" in summary
        grocy.fault("/api/system/info", "400")
        with pytest.raises(ConfigError, match="base URL"):
            await check_grocy(client, opts, wait=0)
        grocy.fault("/api/objects/locations/{id}", "500")
        with pytest.raises(ConfigError, match="cannot read Grocy locations"):
            await check_grocy(client, opts, wait=0)
    finally:
        await client.close()


async def test_check_grocy_not_grocy() -> None:
    from aiohttp import web

    async def html(request: web.Request) -> web.Response:
        return web.Response(text="<html>router login</html>", content_type="text/html")

    app = web.Application()
    app.router.add_get("/api/system/info", html)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    client = GrocyClient(f"http://127.0.0.1:{port}", "k")
    try:
        with pytest.raises(ConfigError, match="pointing at Grocy"):
            await check_grocy(client, make_options(), wait=0)
    finally:
        await client.close()
        await runner.cleanup()


def test_main_bad_options(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PANTRY_HUB_OPTIONS", str(tmp_path / "missing.json"))
    assert main() == 1
    bad = tmp_path / "options.json"
    bad.write_text(json.dumps({"reader_host": ""}))
    monkeypatch.setenv("PANTRY_HUB_OPTIONS", str(bad))
    assert main() == 1


def test_main_runs_until_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = tmp_path / "options.json"
    f.write_text(
        json.dumps(
            {
                "reader_host": "127.0.0.1",
                "grocy_url": "http://127.0.0.1:9",
                "grocy_api_key": "k",
                "grocy_location_id": 1,
                "log_level": "debug",
            }
        )
    )
    monkeypatch.setenv("PANTRY_HUB_OPTIONS", str(f))
    monkeypatch.setenv("PANTRY_HUB_DATA", str(tmp_path))
    monkeypatch.setenv("PANTRY_HUB_GROCY_WAIT", "0")
    assert main() == 1
    setup_logging("info")


def test_version() -> None:
    assert app_version() == "0.3.1"
