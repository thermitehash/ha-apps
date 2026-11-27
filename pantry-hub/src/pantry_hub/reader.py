"""Reader control over TCP (command mode) with aiochainway >= 0.3.0.

* Every window (continuous or sweep) is
  ``async with client.inventory(duration=…) as sweep: async for tag in sweep``.
  Leaving the block always sends stop; ``sweep.result`` says how it ended.
* A sweep counts toward departures only if ``sweep.result.completed`` (the
  duration elapsed while the stream was iterated). Pantry Hub does no
  completion inference of its own: a reader drop is ``connection_lost``, an
  abort (door event, "sweep now") is ``stopped``, neither is completed.
  A missing stop acknowledgement doesn't change that; it is logged.
* Connections (``reader_connection``):
  - ``per_window`` (default, until hardware validation §E.2 says whether the
    UR4 keeps idle TCP sessions): connect with ``stop_on_connect`` and
    ``connect(retries=3)`` for each window, disconnect afterwards. A
    continuous window holds its one connection for its whole duration.
  - ``persistent``: one connection kept open with ``keepalive_interval_s``
    (60 s) and ``auto_reconnect``.
* Between windows the hub calls :meth:`ReaderSession.probe` (connect + ping
  + disconnect, or a ping on the persistent connection) to keep
  ``reader_online`` honest.
* Antennas and power (``reader_antennas``, ``reader_power_dbm``): on every
  new connection, before any inventory, the antenna mask and per-antenna
  power are read; if they differ they're set (antennas with the save flag,
  power per enabled antenna) and read back. Nothing is written to a reader
  that already matches. No inventory runs until the read-back matches.
* The reader's trigger mode (work mode 2) is never used.
* The device unique ID is the reader's MAC from ``aiochainway.discover()``
  (passive listen on UDP 1111), falling back to ``host:port``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import logging
import re
import time
from typing import Any, Literal

from aiochainway import (
    ChainwayError,
    SweepResult,
    TagRead,
    UR4Client,
    WorkMode,
    discover,
)

from .reader_config import ReaderConfig, ReaderConfigError, ensure_config

_LOGGER = logging.getLogger(__name__)

DEFAULT_TCP_PORT = 8888
CONNECT_TIMEOUT = 5.0
CONNECT_RETRIES = 3
KEEPALIVE_INTERVAL_S = 60.0

ConnectionMode = Literal["per_window", "persistent"]


TagFn = Callable[[TagRead], None]
#: ``factory(host, port, **client_kwargs) -> UR4Client`` (tests point it at FakeUR4).
ClientFactory = Callable[..., UR4Client]
DiscoverFn = Callable[[float], Awaitable[list[object]]]


@dataclass(frozen=True, slots=True)
class WindowResult:
    """Outcome of one inventory window."""

    completed: bool
    tags: int
    duration: float
    error: str | None = None
    end_reason: str | None = None
    stop_acknowledged: bool | None = None
    unique_count: int = 0


def _default_client(host: str, port: int, **kwargs: Any) -> UR4Client:
    return UR4Client(host, port, **kwargs)


class ReaderSession:
    """Runs inventory windows against one UR4 in command mode."""

    def __init__(
        self,
        host: str,
        port: int = DEFAULT_TCP_PORT,
        *,
        connection: ConnectionMode = "per_window",
        client_factory: ClientFactory = _default_client,
        timeout: float = CONNECT_TIMEOUT,
        keepalive_interval_s: float = KEEPALIVE_INTERVAL_S,
        connect_retries: int = CONNECT_RETRIES,
        config: ReaderConfig | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Create; nothing connects until a window or probe runs."""
        self.host = host
        self.port = port
        self.connection = connection
        self.timeout = timeout
        self.keepalive_interval_s = keepalive_interval_s
        self.connect_retries = connect_retries
        self._factory = client_factory
        self._client: UR4Client | None = None  # persistent mode only
        #: True after a window/probe reached the reader, False after a failure.
        self.online: bool | None = None
        self.last_error: str | None = None
        self.version: str | None = None
        self.mode_checked = False
        self.last_result: SweepResult | None = None
        #: Wanted antennas/power (None: leave the reader as it is).
        self.config = config
        self._clock = clock
        #: The persistent connection's settings were verified since it opened.
        self._config_verified = False
        #: Verified on the reader: enabled antennas and their power.
        self.antennas_enabled: list[int] | None = None
        self.power_dbm: int | None = None
        #: When the settings last verified (wall time) and the last failure.
        self.config_applied: float | None = None
        self.last_config_error: str | None = None
        #: Number of setting writes sent (tests: none on a matching reader).
        self.config_writes = 0

    # Connections -----------------------------------------------------------------

    def _new_client(self) -> UR4Client:
        kwargs: dict[str, Any] = {"timeout": self.timeout, "stop_on_connect": True}
        if self.connection == "persistent":
            kwargs["keepalive_interval_s"] = self.keepalive_interval_s
            kwargs["auto_reconnect"] = True
            kwargs["on_reconnect"] = self._on_reconnect
        return self._factory(self.host, self.port, **kwargs)

    def _on_reconnect(self) -> None:
        # auto_reconnect opened a new connection: check the settings again.
        self._config_verified = False

    async def _acquire(self, *, configure: bool = True) -> UR4Client:
        """Return a connected client (new per window, or the persistent one).

        With ``configure``, the antenna/power settings are verified (and
        applied if needed) on every new connection before it's returned.
        """
        fresh = True
        if self.connection == "persistent":
            if self._client is None:
                self._client = self._new_client()
            if not self._client.connected:
                self._config_verified = False
                await self._client.connect(retries=self.connect_retries)
            client = self._client
            fresh = not self._config_verified
        else:
            client = self._new_client()
            try:
                await client.connect(retries=self.connect_retries)
            except BaseException:
                await client.close()
                raise
        try:
            if not self.mode_checked:
                await self._ensure_command_mode(client)
            if configure and fresh and self.config is not None:
                await self._ensure_config(client, self.config)
                self._config_verified = True
        except BaseException:
            await self._release(client, failed=True)
            raise
        return client

    async def _ensure_config(self, client: UR4Client, want: ReaderConfig) -> None:
        """Verify (and apply if needed) antennas + power; record the outcome."""
        try:
            applied = await ensure_config(client, want)
        except ReaderConfigError as err:
            self.last_config_error = str(err) or type(err).__name__
            raise
        self.config_writes += applied.writes
        self.antennas_enabled = applied.antennas
        self.power_dbm = applied.power_dbm
        self.config_applied = self._clock()
        self.last_config_error = None

    async def _release(self, client: UR4Client, *, failed: bool) -> None:
        if self.connection == "persistent" and not failed:
            return  # keep it (keepalive + auto_reconnect look after it)
        await client.close()
        if client is self._client:
            self._client = None

    async def close(self) -> None:
        """Close the persistent connection, if any."""
        if self._client is not None:
            await self._client.close()
            self._client = None

    async def _ensure_command_mode(self, client: UR4Client) -> None:
        """Put the reader in command mode if it is in auto or trigger mode."""
        self.version = await client.get_version()
        mode = await client.get_work_mode()
        if mode is not WorkMode.COMMAND:
            _LOGGER.warning(
                "Reader is in %s mode; switching to command mode for "
                "door-driven reading",
                mode.name.lower(),
            )
            await client.set_work_mode(WorkMode.COMMAND)
        self.mode_checked = True

    def _failed(self, err: BaseException) -> str:
        self.online = False
        self.last_error = str(err) or type(err).__name__
        self.mode_checked = False  # re-check after the reader comes back
        self._config_verified = False
        return self.last_error

    # Windows -------------------------------------------------------------------

    async def run_window(self, duration: float, on_tag: TagFn) -> WindowResult:
        """Inventory for ``duration`` seconds; feed every tag to ``on_tag``.

        Completion comes from ``sweep.result.completed`` only. Cancelling the
        task aborts the window (stop is sent; result ``stopped``).
        """
        tags = 0
        try:
            client = await self._acquire()
        except ReaderConfigError as err:
            error = self._failed(err)
            _LOGGER.warning(
                "Reader antenna settings could not be applied: %s (no inventory)",
                error,
            )
            return WindowResult(False, 0, 0.0, error, "config_failed")
        except (ChainwayError, OSError) as err:
            error = self._failed(err)
            _LOGGER.warning("Reader unreachable: %s", error)
            return WindowResult(False, 0, 0.0, error, "connect_failed")
        failed = False
        sweep = client.inventory(duration=duration)
        try:
            async with sweep:
                async for tag in sweep:
                    tags += 1
                    on_tag(tag)
        except (ChainwayError, OSError) as err:
            failed = True
            error = self._failed(err)
            _LOGGER.warning(
                "Reader window ended early after %d tag(s): %s", tags, error
            )
        finally:
            # Set before release so an aborted (cancelled) window records it.
            self.last_result = sweep.result
            await self._release(client, failed=failed)
        result = sweep.result
        if result is None:  # pragma: no cover - aiochainway always sets it
            return WindowResult(False, tags, 0.0, self.last_error, "unknown")
        if not failed:
            self.online = True
            self.last_error = None
        if result.completed and not result.stop_acknowledged:
            _LOGGER.info(
                "Reader didn't acknowledge stop after a %.0f s window; it still "
                "counts (the duration elapsed)",
                duration,
            )
        return WindowResult(
            completed=result.completed,
            tags=tags,
            duration=result.elapsed or 0.0,
            error=result.error or (self.last_error if failed else None),
            end_reason=str(result.end_reason),
            stop_acknowledged=result.stop_acknowledged,
            unique_count=result.unique_count,
        )

    async def probe(self) -> float | None:
        """Check the reader between windows: ping round trip in seconds, or None."""
        try:
            client = await self._acquire(configure=False)
        except (ChainwayError, OSError) as err:
            self._failed(err)
            return None
        failed = False
        try:
            rtt = await client.ping()
        except (ChainwayError, OSError) as err:
            failed = True
            self._failed(err)
            return None
        finally:
            await self._release(client, failed=failed)
        self.online = True
        self.last_error = None
        return rtt


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
