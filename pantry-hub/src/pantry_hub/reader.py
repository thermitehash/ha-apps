"""Reader control over TCP (command mode) with aiochainway.

* Windows (continuous or sweep) are ``UR4Client.inventory(duration=…)`` streams.
  The reader's own trigger mode (work mode 2) is never used.
* A window is *completed* only if the TCP session stayed up for the whole
  window and the inventory ended normally (tags, or an explicit empty
  result). Any reader error or cancellation makes it *aborted*.
* The device unique ID is the reader's MAC from ``aiochainway.discover()``
  (passive listen on UDP 1111), falling back to ``host:port``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import logging
import re

from aiochainway import (
    ChainwayError,
    TagRead,
    UR4Client,
    WorkMode,
    discover,
)

_LOGGER = logging.getLogger(__name__)

DEFAULT_TCP_PORT = 8888
CONNECT_TIMEOUT = 5.0

TagFn = Callable[[TagRead], None]
ClientFactory = Callable[[str, int, float], UR4Client]
DiscoverFn = Callable[[float], Awaitable[list[object]]]


@dataclass(frozen=True, slots=True)
class WindowResult:
    """Outcome of one inventory window."""

    completed: bool
    tags: int
    duration: float
    error: str | None = None


def _default_client(host: str, port: int, timeout: float) -> UR4Client:
    return UR4Client(host, port, timeout=timeout)


class ReaderSession:
    """Runs inventory windows against one UR4 in command mode."""

    def __init__(
        self,
        host: str,
        port: int = DEFAULT_TCP_PORT,
        *,
        client_factory: ClientFactory = _default_client,
        timeout: float = CONNECT_TIMEOUT,
    ) -> None:
        """Create; nothing connects until a window runs."""
        self.host = host
        self.port = port
        self.timeout = timeout
        self._factory = client_factory
        #: True after a window or check reached the reader, False after a failure.
        self.online: bool | None = None
        self.last_error: str | None = None
        self.version: str | None = None
        self.mode_checked = False

    async def ensure_command_mode(self) -> None:
        """Put the reader in command mode if it is in auto or trigger mode.

        Door-driven reading needs command mode (the hub starts and stops
        inventories itself). Trigger mode (2) is never used.
        """
        async with self._factory(self.host, self.port, self.timeout) as client:
            self.version = await client.get_version()
            mode = await client.get_work_mode()
            if mode is not WorkMode.COMMAND:
                _LOGGER.warning(
                    "Reader is in %s mode; switching to command mode for "
                    "door-driven reading",
                    mode.name.lower(),
                )
                await client.set_work_mode(WorkMode.COMMAND)
            await client.stop_inventory()  # clean slate after a restart
        self.mode_checked = True
        self.online = True

    async def run_window(self, duration: float, on_tag: TagFn) -> WindowResult:
        """Inventory for ``duration`` seconds; feed every tag to ``on_tag``.

        Cancelling the task aborts the window (the reader is told to stop).
        """
        loop = asyncio.get_running_loop()
        started = loop.time()
        tags = 0
        try:
            if not self.mode_checked:
                await self.ensure_command_mode()
            async with self._factory(self.host, self.port, self.timeout) as client:
                async for tag in client.inventory(duration=duration):
                    tags += 1
                    on_tag(tag)
        except (ChainwayError, OSError) as err:
            self.online = False
            self.last_error = str(err) or type(err).__name__
            self.mode_checked = False  # re-check after the reader comes back
            _LOGGER.warning(
                "Reader window aborted after %d tag(s): %s", tags, self.last_error
            )
            return WindowResult(False, tags, loop.time() - started, self.last_error)
        self.online = True
        self.last_error = None
        return WindowResult(True, tags, loop.time() - started)


_MAC_RE = re.compile(r"[^0-9a-f]")


async def reader_unique_id(
    host: str,
    port: int,
    *,
    discover_fn: DiscoverFn | None = None,
    timeout: float = 5.0,
) -> tuple[str, str]:
    """Return ``(unique_id, source)`` for the reader.

    Listens for the reader's UDP 1111 announcement (aiochainway.discover) and
    uses its MAC if one comes from ``host``; otherwise ``host:port``.
    Announcements are broadcasts: they only arrive if the reader and Home
    Assistant share a broadcast domain (and UDP 1111 is mapped).
    """
    try:
        found = await (discover_fn or discover)(timeout)
    except OSError as err:
        _LOGGER.info("Reader discovery on UDP 1111 unavailable: %s", err)
        found = []
    for reader in found:
        ip = getattr(reader, "ip", None)
        mac = getattr(reader, "mac", None)
        if ip == host and isinstance(mac, str) and mac:
            return "ur4_" + _MAC_RE.sub("", mac.lower()), "mac"
    return f"ur4_{host}:{port}", "host"
