"""Antenna mask + power enforcement via aiochainway's ``UR4Client.ensure_config()``.

aiochainway (>= 0.4.0) reads the mask and per-antenna power, writes only what
differs (the antenna mask with the save flag, power per requested antenna),
reads everything back and reports whether it verified. This module only maps
Pantry Hub's options onto that call and its result onto
:class:`ReaderConfigError` / :class:`AppliedConfig`.

The UR4 can't detect whether an antenna is connected (its SDK has no such
command); see :mod:`pantry_hub.dead_ports` for the read-count based warning.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from aiochainway import ChainwayError, UR4Client


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


async def ensure_config(client: UR4Client, want: ReaderConfig) -> AppliedConfig:
    """Make the reader match ``want``; raise :class:`ReaderConfigError` if it won't.

    A failed write (or any reader error) and a read-back that doesn't verify
    both raise; a reader that already matches gets no writes.
    """
    try:
        result = await client.ensure_config(sorted(want.antennas), want.power_dbm)
    except ChainwayError as err:
        raise ReaderConfigError(str(err) or type(err).__name__) from err
    if not result.verified or result.after is None:
        raise ReaderConfigError(
            "; ".join(result.mismatches) or "read-back did not verify"
        )
    return AppliedConfig(
        antennas=sorted(result.after.antennas),
        power_dbm=want.power_dbm,
        writes=len(result.writes),
    )
