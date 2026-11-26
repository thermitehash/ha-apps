"""Per-EPC presence state machine (pure: no I/O, time is passed in).

States::

    unknown ──read──▶ arriving ──N reads within window──▶ present
                         │                                  │  ▲
                 window expires                     unseen  │  │ read
                         ▼                     missing_after│  │
                      unknown                               ▼  │
                                                          missing
                                                            │
                               unseen depart_after (and door gate open)
                                                            ▼
                                                         departed ──read──▶ arriving

``arrived`` is emitted on arriving → present; ``departed`` on missing →
departed. Events alternate per EPC, starting with ``arrived`` (an EPC restored
after a restart starts in ``present`` because its ``arrived`` already happened
before the restart).
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal


class State(StrEnum):
    """Presence state of one EPC."""

    UNKNOWN = "unknown"
    ARRIVING = "arriving"
    PRESENT = "present"
    MISSING = "missing"
    DEPARTED = "departed"


class Door(StrEnum):
    """Pantry door state as reported over MQTT."""

    UNKNOWN = "unknown"
    OPEN = "open"
    CLOSED = "closed"


EventKind = Literal["arrived", "departed"]


@dataclass(frozen=True, slots=True)
class PresenceEvent:
    """An EPC arrived or departed."""

    kind: EventKind
    epc: str
    at: float


@dataclass(frozen=True, slots=True)
class PresenceConfig:
    """Thresholds (seconds)."""

    arrive_reads: int = 3
    arrive_window_s: float = 10.0
    depart_after_s: float = 900.0
    depart_requires_door_closed: bool = False

    @property
    def missing_after_s(self) -> float:
        """Unseen this long → ``missing`` (informational; no event)."""
        return min(60.0, self.depart_after_s / 2)

    @property
    def door_settle_s(self) -> float:
        """How long the door must have been closed before a departure counts.

        Gives the reader one arrival window to re-read tags that a person in
        the doorway was shadowing.
        """
        return self.arrive_window_s


@dataclass(slots=True)
class Track:
    """What the machine knows about one EPC."""

    epc: str
    state: State
    last_seen: float
    reads: deque[float] = field(default_factory=deque)


class PresenceMachine:
    """Turn raw reads into debounced ``arrived`` / ``departed`` events."""

    def __init__(self, config: PresenceConfig) -> None:
        """Create an empty machine."""
        if config.arrive_reads < 1:
            raise ValueError("arrive_reads must be at least 1")
        if config.depart_after_s <= config.arrive_window_s:
            raise ValueError("depart_after_s must be longer than arrive_window_s")
        self.config = config
        self._tracks: dict[str, Track] = {}
        self.door = Door.UNKNOWN
        self._door_closed_since: float | None = None

    # Queries -----------------------------------------------------------------

    def state(self, epc: str) -> State:
        """Return the state of ``epc`` (``unknown`` if never seen)."""
        track = self._tracks.get(epc)
        return State.UNKNOWN if track is None else track.state

    def tracks(self) -> dict[str, Track]:
        """Return a shallow copy of every tracked EPC."""
        return dict(self._tracks)

    def in_pantry(self) -> list[str]:
        """EPCs currently ``present`` or ``missing`` (arrived, not departed)."""
        return [
            t.epc
            for t in self._tracks.values()
            if t.state in (State.PRESENT, State.MISSING)
        ]

    # Inputs ------------------------------------------------------------------

    def restore(self, epcs: Iterable[str], now: float) -> None:
        """Mark EPCs that were in the pantry before a restart as present.

        Their departure clock starts at ``now``, so nothing departs until one
        full ``depart_after_s`` has passed without a read.
        """
        for epc in epcs:
            self._tracks[epc] = Track(epc=epc, state=State.PRESENT, last_seen=now)

    def read(self, epc: str, now: float) -> list[PresenceEvent]:
        """Record one read of ``epc`` at ``now``."""
        track = self._tracks.get(epc)
        if track is None or track.state in (State.UNKNOWN, State.DEPARTED):
            track = Track(epc=epc, state=State.ARRIVING, last_seen=now)
            self._tracks[epc] = track
        else:
            track.last_seen = max(track.last_seen, now)
        if track.state is State.ARRIVING:
            track.reads.append(now)
            self._prune_reads(track, now)
            if len(track.reads) >= self.config.arrive_reads:
                track.state = State.PRESENT
                track.reads.clear()
                return [PresenceEvent("arrived", epc, now)]
            return []
        track.state = State.PRESENT  # present or missing → present
        return []

    def set_door(self, door: Door, now: float) -> None:
        """Update the pantry door state."""
        if door is Door.CLOSED and self.door is not Door.CLOSED:
            self._door_closed_since = now
        elif door is not Door.CLOSED:
            self._door_closed_since = None
        self.door = door

    def tick(self, now: float) -> list[PresenceEvent]:
        """Advance timers; return departures."""
        events: list[PresenceEvent] = []
        cfg = self.config
        for epc in list(self._tracks):
            track = self._tracks[epc]
            unseen = now - track.last_seen
            if track.state is State.ARRIVING:
                self._prune_reads(track, now)
                if not track.reads:
                    del self._tracks[epc]
            elif track.state is State.PRESENT and unseen > cfg.missing_after_s:
                track.state = State.MISSING
            if (
                track.state is State.MISSING
                and unseen >= cfg.depart_after_s
                and self._door_allows_departure(now)
            ):
                del self._tracks[epc]
                events.append(PresenceEvent("departed", epc, now))
        return events

    # Internals ---------------------------------------------------------------

    def _prune_reads(self, track: Track, now: float) -> None:
        horizon = now - self.config.arrive_window_s
        while track.reads and track.reads[0] < horizon:
            track.reads.popleft()

    def _door_allows_departure(self, now: float) -> bool:
        if not self.config.depart_requires_door_closed:
            return True
        if self.door is not Door.CLOSED or self._door_closed_since is None:
            return False
        return now - self._door_closed_since >= self.config.door_settle_s
