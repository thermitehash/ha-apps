"""Shared fixtures: fake clock, fake Grocy, in-process MQTT broker, store."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
import logging
import os
from pathlib import Path
import socket
from typing import Any

from amqtt.broker import Broker
from hypothesis import settings
import pytest

from pantry_hub.config import Options
from pantry_hub.store import Store

from .fake_grocy import API_KEY, FakeGrocy
from .fake_ha import FakeHomeAssistant

settings.register_profile("ci", max_examples=300, deadline=None)
settings.register_profile("dev", max_examples=60, deadline=None)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))

logging.getLogger("amqtt").setLevel(logging.WARNING)
logging.getLogger("transitions").setLevel(logging.WARNING)


class FakeClock:
    """Manually advanced clock (monotonic and wall share one timeline)."""

    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


async def eventually(
    check: Callable[[], bool | Awaitable[bool]],
    timeout: float = 5.0,
    step: float = 0.02,
) -> None:
    """Wait until ``check()`` is true (no fixed sleeps in tests)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        result = check()
        if not isinstance(result, bool):
            result = await result
        if result:
            return
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(step)


def free_port(kind: int = socket.SOCK_STREAM) -> int:
    with socket.socket(socket.AF_INET, kind) as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


def make_options(**overrides: Any) -> Options:
    base: dict[str, Any] = {
        "reader_host": "127.0.0.1",
        "grocy_url": "http://127.0.0.1:1",
        "grocy_api_key": API_KEY,
        "grocy_location_id": 1,
        "arrive_reads": 2,
        "arrive_window_s": 5.0,
        "depart_after_s": 60.0,
        "reading_mode": "continuous",
        "udp_port": 0,
    }
    base.update(overrides)
    return Options(**base)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[Store]:
    s = await Store.open(tmp_path / "test.db")
    yield s
    await s.close()


@pytest.fixture
async def ha() -> AsyncIterator[FakeHomeAssistant]:
    h = FakeHomeAssistant()
    await h.start()
    yield h
    await h.stop()


@pytest.fixture
async def grocy() -> AsyncIterator[FakeGrocy]:
    g = FakeGrocy()
    await g.start()
    yield g
    await g.stop()


class BrokerHandle:
    def __init__(self, port: int) -> None:
        self.port = port
        self.broker: Broker | None = None

    async def start(self) -> None:
        self.broker = Broker(
            {
                "listeners": {
                    "default": {"type": "tcp", "bind": f"127.0.0.1:{self.port}"}
                },
                "plugins": {
                    "amqtt.plugins.authentication.AnonymousAuthPlugin": {
                        "allow_anonymous": True
                    }
                },
            }
        )
        await self.broker.start()

    async def stop(self) -> None:
        if self.broker is not None:
            await self.broker.shutdown()
            self.broker = None


@pytest.fixture
async def broker() -> AsyncIterator[BrokerHandle]:
    handle = BrokerHandle(free_port())
    await handle.start()
    yield handle
    await handle.stop()
