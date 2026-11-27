"""Per-EPC presence state machine (pure: no I/O, time is passed in).

States::

    unknown ──read──▶ arriving ──N reads within window──▶ present
                         │                                  │  ▲
                 window expires                   unseen /  │  │ read
                         ▼                   missed sweep   │  │
                      unknown                               ▼  │
                                                          missing
                                                            │
             unseen reading time ≥ depart_after_s  or  missed N completed sweeps
                                                            ▼
                                                         departed ──read──▶ arriving

``arrived`` is emitted on arriving → present; ``departed`` on missing/present →
departed. Events alternate per EPC, starting with ``arrived`` (an EPC restored
after a restart starts in ``present`` because its ``arrived`` already happened
before the restart).

Two ways to depart:

* **Unseen timer** (``depart_after_s``): counts only *reading time*. The hub
  calls :meth:`tick` with ``accrue=True`` while the reader is reading
  continuously (door open, or the UDP/auto fallback mode) and ``accrue=False``
  otherwise, so time with the reader idle never counts as "not seen".
* **Missed sweeps** (``depart_after_missed_sweeps``): a tag that is present
  but absent from N consecutive *completed* sweeps departs. An aborted sweep
  (reader dropped, door event) never counts as a miss.

After a restart, ``min_sweeps_before_departure`` completed sweeps must happen
before anything may depart.
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
    """Pantry door state (from the Home Assistant door entity)."""

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
    """Thresholds (seconds unless noted)."""

    arrive_reads: int = 3
    arrive_window_s: float = 10.0
    depart_after_s: float = 900.0
    depart_requires_door_closed: bool = False
    #: Consecutive completed sweeps a present tag may be missing from (0 = off).
    depart_after_missed_sweeps: int = 0
    #: Completed sweeps needed after start before anything may depart.
    min_sweeps_before_departure: int = 0

    @property
    def missing_after_s(self) -> float:
        """Unseen reading time → ``missing`` (informational; no event)."""
        return min(60.0, self.depart_after_s / 2)

    @property
    def door_settle_s(self) -> float:
        """How long the door must have been closed before a timer departure counts."""
        return self.arrive_window_s


@dataclass(slots=True)
class Track:
    """What the machine knows about one EPC."""

    epc: str
    state: State
    last_seen: float
    reads: deque[float] = field(default_factory=deque)
    #: Reading time accrued since the last read (the unseen timer).
    unseen: float = 0.0
    #: Time up to which ``unseen`` has been accounted for.
    mark: float = 0.0
    #: Consecutive completed sweeps this present tag was missing from.
    missed: int = 0


class PresenceMachine:
    """Turn raw reads into debounced ``arrived`` / ``departed`` events."""

    def __init__(self, config: PresenceConfig) -> None:
        """Create an empty machine."""
        if config.arrive_reads < 1:
            raise ValueError("arrive_reads must be at least 1")
        if config.depart_after_s <= config.arrive_window_s:
            raise ValueError("depart_after_s must be longer than arrive_window_s")
        if config.depart_after_missed_sweeps < 0:
            raise ValueError("depart_after_missed_sweeps must not be negative")
        self.config = config
        self._tracks: dict[str, Track] = {}
        self.door = Door.UNKNOWN
        self._door_closed_since: float | None = None
        self._sweep_seen: set[str] | None = None
        #: Completed sweeps since this machine was created (i.e. since start).
        self.sweeps_completed = 0
        self.sweeps_aborted = 0

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

    @property
    def sweeping(self) -> bool:
        """Whether a sweep is in progress."""
        return self._sweep_seen is not None

    @property
    def departures_allowed(self) -> bool:
        """False until enough sweeps completed after start (restart rule)."""
        return self.sweeps_completed >= self.config.min_sweeps_before_departure

    # Inputs ------------------------------------------------------------------

    def restore(self, epcs: Iterable[str], now: float) -> None:
        """Mark EPCs that were in the pantry before a restart as present."""
        for epc in epcs:
            self._tracks[epc] = Track(
                epc=epc, state=State.PRESENT, last_seen=now, mark=now
            )

    def read(self, epc: str, now: float) -> list[PresenceEvent]:
        """Record one read of ``epc`` at ``now``."""
        if self._sweep_seen is not None:
            self._sweep_seen.add(epc)
        track = self._tracks.get(epc)
        if track is None or track.state in (State.UNKNOWN, State.DEPARTED):
            track = Track(epc=epc, state=State.ARRIVING, last_seen=now, mark=now)
            self._tracks[epc] = track
        else:
            track.last_seen = max(track.last_seen, now)
        track.unseen = 0.0
        track.missed = 0
        track.mark = max(track.mark, now)
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
        """Update the pantry door state (timer-departure gating)."""
        if door is Door.CLOSED and self.door is not Door.CLOSED:
            self._door_closed_since = now
        elif door is not Door.CLOSED:
            self._door_closed_since = None
        self.door = door

    def tick(self, now: float, *, accrue: bool = True) -> list[PresenceEvent]:
        """Advance timers; return timer departures.

        ``accrue``: whether the reader was reading continuously since the
        last tick. If not, that time does not count towards ``unseen``.
        """
        events: list[PresenceEvent] = []
        cfg = self.config
        for epc in list(self._tracks):
            track = self._tracks[epc]
            if track.state is State.ARRIVING:
                self._prune_reads(track, now)
                if not track.reads:
                    del self._tracks[epc]
                continue
            if now > track.mark:
                if accrue:
                    track.unseen += now - track.mark
                track.mark = now
            if track.state is State.PRESENT and track.unseen > cfg.missing_after_s:
                track.state = State.MISSING
            if (
                track.state is State.MISSING
                and track.unseen >= cfg.depart_after_s
                and self.departures_allowed
                and self._door_allows_departure(now)
            ):
                del self._tracks[epc]
                events.append(PresenceEvent("departed", epc, now))
        return events

    def start_sweep(self) -> None:
        """Begin a sweep: remember which EPCs are read until it finishes."""
        self._sweep_seen = set()

    def finish_sweep(self, completed: bool, now: float) -> list[PresenceEvent]:
        """End the sweep; return departures (only for a completed sweep).

        A sweep is *completed* when the reader stayed online for the whole
        window and the inventory ended normally (tags, or an explicit empty
        result). Aborted sweeps change nothing.
        """
        seen = self._sweep_seen or set()
        self._sweep_seen = None
        if not completed:
            self.sweeps_aborted += 1
            return []
        self.sweeps_completed += 1
        events: list[PresenceEvent] = []
        limit = self.config.depart_after_missed_sweeps
        for epc in list(self._tracks):
            track = self._tracks[epc]
            if track.state not in (State.PRESENT, State.MISSING):
                continue
            if epc in seen:
                track.missed = 0
                continue
            track.missed += 1
            track.state = State.MISSING
            if limit and track.missed >= limit and self.departures_allowed:
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
