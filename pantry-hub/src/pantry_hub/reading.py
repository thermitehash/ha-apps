"""When to read: the door-driven reading planner (pure: no I/O, time passed in).

Rules (``reading_mode: door``)::

    start ───────────────────────────────▶ sweep (startup)
    door opens ──────────────────────────▶ continuous for open_continuous_s,
                                           then sweep_s every open_sweep_interval_s
    door closes ─────────────────────────▶ sweep_s now (the close sweep), then
    closed ──────────────────────────────▶ sweep_s every closed_sweep_interval_s
    door unavailable / unknown ──────────▶ same as closed (never "open forever")
    "sweep now" button ──────────────────▶ sweep_s now

Any door change aborts the window in progress (an aborted sweep never counts
as a miss). A sweep that failed (reader offline) is retried after
``retry_s`` instead of waiting a whole interval.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .presence import Door


class Activity(StrEnum):
    """What the reader is doing (``sensor.pantry_hub_reader_activity``)."""

    IDLE = "idle"
    CONTINUOUS = "continuous"
    SWEEP = "sweep"


class SweepReason(StrEnum):
    """Why a sweep runs (logged and published)."""

    STARTUP = "startup"
    CLOSE = "door_closed"
    SCHEDULED = "scheduled"
    REQUESTED = "requested"
    RETRY = "retry"


@dataclass(frozen=True, slots=True)
class ReadingConfig:
    """Timing (seconds)."""

    open_continuous_s: float = 600.0
    open_sweep_interval_s: float = 300.0
    sweep_s: float = 45.0
    closed_sweep_interval_s: float = 3600.0
    retry_s: float = 60.0


@dataclass(frozen=True, slots=True)
class Plan:
    """The next thing to do: ``activity`` now for ``duration`` seconds.

    For ``IDLE``, ``duration`` is how long to wait before asking again
    (door changes and "sweep now" wake the loop earlier).
    """

    activity: Activity
    duration: float
    reason: SweepReason | None = None


class ReadingPlanner:
    """Decides continuous windows and sweeps from the door state and the clock."""

    def __init__(self, config: ReadingConfig, now: float) -> None:
        """Create; the first plan is the startup sweep."""
        if config.sweep_s <= 0 or config.open_continuous_s < 0:
            raise ValueError("sweep_s must be positive, open_continuous_s >= 0")
        if config.sweep_s >= min(
            config.open_sweep_interval_s, config.closed_sweep_interval_s
        ):
            raise ValueError("sweep_s must be shorter than both sweep intervals")
        self.config = config
        self.door = Door.UNKNOWN
        self.door_since = now
        # Pending sweeps, tracked separately so one can't swallow another
        # (e.g. a failed startup sweep must not drop a pending close sweep).
        self._startup = True
        self._close = False
        self._requested = False
        self._last_sweep_end: float | None = None
        self._last_sweep_ok = True
        self._continuous_done = False

    # Inputs ------------------------------------------------------------------

    def set_door(self, door: Door, now: float) -> bool:
        """Update the door; return True if the current window must be aborted.

        Opening aborts whatever runs (an open door means continuous reading);
        leaving "open" (closed or unavailable) aborts the continuous window.
        Changes between closed and unknown don't interrupt a sweep.
        """
        if door is self.door:
            return False
        previous = self.door
        self.door = door
        self.door_since = now
        if door is Door.OPEN:
            self._continuous_done = False
        elif previous is Door.OPEN and door is Door.CLOSED:
            self._close = True
        return door is Door.OPEN or previous is Door.OPEN

    def request_sweep(self) -> None:
        """Queue a sweep (the "sweep now" button)."""
        self._requested = True

    def sweep_done(self, completed: bool, now: float) -> None:
        """Record that a sweep ended (completed or aborted)."""
        self._last_sweep_end = now
        self._last_sweep_ok = completed
        self._startup = False  # a failed startup sweep is retried like any other
        if completed:
            self._close = False
            self._requested = False
        # A failed close/requested sweep stays pending and is retried after
        # retry_s (no tight loop while the reader is offline).

    def continuous_done(self) -> None:
        """Record that the open-door continuous window ran to its end."""
        self._continuous_done = True

    # Output ------------------------------------------------------------------

    def _pending_reason(self) -> SweepReason | None:
        if self._startup:
            return SweepReason.STARTUP
        if self._close:
            return SweepReason.CLOSE
        if self._requested:
            return SweepReason.REQUESTED
        return None

    def next(self, now: float) -> Plan:
        """Return what to do now."""
        cfg = self.config
        pending = self._pending_reason()
        if pending is not None:
            if self._last_sweep_ok or self._last_sweep_end is None:
                return Plan(Activity.SWEEP, cfg.sweep_s, pending)
            retry_at = self._last_sweep_end + cfg.retry_s
            if now >= retry_at:
                return Plan(Activity.SWEEP, cfg.sweep_s, pending)
            return Plan(Activity.IDLE, retry_at - now)
        if self.door is Door.OPEN:
            continuous_end = self.door_since + cfg.open_continuous_s
            if not self._continuous_done and now < continuous_end:
                return Plan(Activity.CONTINUOUS, continuous_end - now)
            interval = cfg.open_sweep_interval_s
            start = continuous_end
        else:
            interval = cfg.closed_sweep_interval_s
            start = self.door_since
        if self._last_sweep_end is None:
            due = start
        elif not self._last_sweep_ok:
            due = self._last_sweep_end + min(cfg.retry_s, interval)
        else:
            due = max(start, self._last_sweep_end + interval)
        if now >= due:
            reason = SweepReason.SCHEDULED if self._last_sweep_ok else SweepReason.RETRY
            return Plan(Activity.SWEEP, cfg.sweep_s, reason)
        return Plan(Activity.IDLE, due - now)
