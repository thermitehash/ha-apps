"""Shared fixtures: fake clock, fake Grocy, in-process MQTT broker, store."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
import io
import logging
import os
from pathlib import Path
import socket
import threading
from typing import Any

from _pytest.faulthandler import fault_handler_stderr_fd_key
from aiochainway.testing import FakeUR4, tag_record
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


#: A test (setup + call + teardown) running longer than this dumps every
#: asyncio task's stack: faulthandler only shows threads, not which coroutine
#: is stuck (e.g. a fixture teardown waiting forever).
HANG_DUMP_AFTER_S = float(os.environ.get("PANTRY_HUB_HANG_DUMP_S", "120"))


def _print_await_chain(task: asyncio.Task[Any], buf: io.StringIO) -> None:
    """Follow awaits, including into async generators (fixture teardowns)."""
    obj: Any = task.get_coro()
    seen: set[int] = set()
    while obj is not None and id(obj) not in seen:
        seen.add(id(obj))
        frame = (
            getattr(obj, "cr_frame", None)
            or getattr(obj, "ag_frame", None)
            or getattr(obj, "gi_frame", None)
        )
        if frame is not None:
            code = frame.f_code
            print(
                f"    await chain: {code.co_filename}:{frame.f_lineno} "
                f"in {code.co_qualname}",
                file=buf,
            )
            # pytest-asyncio awaits gen_obj.__anext__(): follow the generator.
            gen = frame.f_locals.get("gen_obj")
            if gen is not None and getattr(gen, "ag_await", None) is not None:
                obj = gen
                continue
        nxt = (
            getattr(obj, "cr_await", None)
            or getattr(obj, "ag_await", None)
            or getattr(obj, "gi_yieldfrom", None)
        )
        if nxt is None:
            print(f"    waiting on: {obj!r}", file=buf)
        obj = nxt


def _dump_asyncio_tasks(
    name: str, loops: list[asyncio.AbstractEventLoop], fd: int
) -> None:
    # Written to the stderr copy pytest gives faulthandler: captured output of
    # a test that never finishes would never be shown.
    buf = io.StringIO()
    print(f"\n=== {name} still running after {HANG_DUMP_AFTER_S:.0f} s ===", file=buf)
    for loop in loops:
        try:
            tasks = list(asyncio.all_tasks(loop))
        except RuntimeError:  # the set changed while we read it; try once more
            tasks = list(asyncio.all_tasks(loop))
        for task in tasks:
            print(asyncio.format_call_graph(task), file=buf)
            _print_await_chain(task, buf)
    os.write(fd, buf.getvalue().encode())


@pytest.fixture(autouse=True)
def _hang_watchdog(request: pytest.FixtureRequest) -> Any:
    loops: list[asyncio.AbstractEventLoop] = []
    original = asyncio.BaseEventLoop.run_forever

    def tracking(self: asyncio.BaseEventLoop) -> None:
        if self not in loops:
            loops.append(self)
        original(self)

    asyncio.BaseEventLoop.run_forever = tracking  # type: ignore[method-assign]
    fd = request.config.stash.get(fault_handler_stderr_fd_key, 2)
    timer = threading.Timer(
        HANG_DUMP_AFTER_S, _dump_asyncio_tasks, (request.node.nodeid, loops, fd)
    )
    timer.daemon = True
    timer.start()
    yield
    timer.cancel()
    asyncio.BaseEventLoop.run_forever = original  # type: ignore[method-assign]


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
async def fake() -> AsyncIterator[FakeUR4]:
    """aiochainway's own fake UR4 (TCP command port), reading one tag."""
    async with FakeUR4() as f:
        f.tags = [tag_record("E28011700000020F1A2B3CFF", rssi_dbm=-70)]
        f.tag_interval = 0.01
        yield f


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
            # amqtt's shutdown ends with asyncio Server.wait_closed(), which
            # (Python >= 3.12) waits for every client connection to close; it
            # never closes them itself, so a leftover socket (a client mid
            # connect or reconnect when the broker stops) hung the teardown
            # forever. Stop listening and close the clients first.
            for server in self.broker._servers.values():
                instance = server.instance
                if isinstance(instance, asyncio.Server):
                    instance.close()
                    instance.close_clients()
            await self.broker.shutdown()
            self.broker = None


@pytest.fixture
async def broker() -> AsyncIterator[BrokerHandle]:
    handle = BrokerHandle(free_port())
    await handle.start()
    yield handle
    await handle.stop()
