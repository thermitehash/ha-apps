"""Antenna mask + power enforcement: a temporary adapter over aiochainway v0.3.0.

aiochainway v0.4.0 will provide ``UR4Client.ensure_config()`` and
``get_antenna_status()`` (read, write only what differs, read back, verify).
It isn't tagged yet, so this module implements the same contract with the
v0.3.0 calls (``get_antennas``, ``get_power_all``, ``set_antennas``,
``set_power``).

When v0.4.0 is tagged: pin it, make :func:`ensure_config` call the library's
``ensure_config()`` (mapping its result to :class:`AppliedConfig` and its
errors to :class:`ReaderConfigError`), and delete the read/compare/verify code
below. :class:`~pantry_hub.reader.ReaderSession` only calls
:func:`ensure_config`, so nothing else changes.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import logging

from aiochainway import ChainwayError, UR4Client

_LOGGER = logging.getLogger(__name__)


class ReaderConfigError(ChainwayError):
    """The antenna mask or power couldn't be set or didn't read back as asked."""


@dataclass(frozen=True, slots=True)
class ReaderConfig:
    """Antenna ports to enable and the power for each of them."""

    antennas: frozenset[int]
    power_dbm: int

    @classmethod
    def of(cls, antennas: Iterable[int], power_dbm: int) -> ReaderConfig:
        """Build from a list of antenna numbers."""
        return cls(frozenset(antennas), power_dbm)


@dataclass(frozen=True, slots=True)
class AppliedConfig:
    """What the reader reported after :func:`ensure_config` (verified)."""

    antennas: list[int]
    power_dbm: int
    #: Setting commands sent (0 when the reader already matched).
    writes: int


async def _read(client: UR4Client) -> tuple[frozenset[int], dict[int, int]]:
    antennas = (await client.get_antennas()).enabled
    power = {p.antenna: round(p.power) for p in await client.get_power_all()}
    return antennas, power


def _mismatch(
    want: ReaderConfig, antennas: frozenset[int], power: dict[int, int]
) -> list[str]:
    problems = []
    if antennas != want.antennas:
        problems.append(f"antennas {sorted(antennas)}, wanted {sorted(want.antennas)}")
    bad = {
        a: power.get(a) for a in sorted(want.antennas) if power.get(a) != want.power_dbm
    }
    if bad:
        problems.append(f"power {bad}, wanted {want.power_dbm} dBm")
    return problems


async def ensure_config(client: UR4Client, want: ReaderConfig) -> AppliedConfig:
    """Make the reader match ``want``; write only what differs; verify.

    Antennas are set with the save flag (aiochainway always sends it); power
    is set per enabled antenna. Raises :class:`ReaderConfigError` if a
    command fails or the read-back doesn't match.
    """
    writes = 0
    try:
        antennas, power = await _read(client)
        if _mismatch(want, antennas, power):
            if antennas != want.antennas:
                _LOGGER.warning(
                    "Reader antennas %s, configured %s: setting them (saved)",
                    sorted(antennas),
                    sorted(want.antennas),
                )
                await client.set_antennas(want.antennas)
                writes += 1
            for antenna in sorted(want.antennas):
                if power.get(antenna) != want.power_dbm:
                    _LOGGER.warning(
                        "Reader antenna %d power %s dBm, configured %d: setting it",
                        antenna,
                        power.get(antenna),
                        want.power_dbm,
                    )
                    await client.set_power(antenna, want.power_dbm)
                    writes += 1
            antennas, power = await _read(client)
            problems = _mismatch(want, antennas, power)
            if problems:
                raise ReaderConfigError("read back " + "; ".join(problems))
    except ReaderConfigError:
        raise
    except ChainwayError as err:
        raise ReaderConfigError(str(err) or type(err).__name__) from err
    return AppliedConfig(sorted(antennas), want.power_dbm, writes)
