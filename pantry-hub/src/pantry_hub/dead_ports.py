"""Dead antenna port warning, from per-antenna read counts of completed sweeps.

The UR4 can't detect whether an antenna is connected, so a port that never
reads while others do (or while tags are known to be in the pantry) is the
best available hint of a loose or broken cable. Warning only: this never
blocks reading and never changes presence.

Rules, over the last :attr:`DeadPortMonitor.sweeps` (5) *completed* sweeps
(aborted sweeps aren't recorded):

* 2+ enabled ports: a port with 0 reads in each of them while the other
  ports read at least :attr:`DeadPortMonitor.others_min` (10) times in total
  over the same sweeps.
* 1 enabled port: 0 reads in each of them while tags were in the pantry
  (``present_tags > 0``) at the start of any of those sweeps. The count is
  taken at the start because with a dead single port every tag departs after
  ``depart_after_missed_sweeps`` misses, long before the fifth sweep.

A flagged port clears as soon as it reads again, in any window.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

SWEEPS = 5
OTHERS_MIN = 10


@dataclass(frozen=True, slots=True)
class _Sweep:
    counts: dict[int, int]
    present_at_start: int


@dataclass(slots=True)
class DeadPortMonitor:
    """Track per-port reads; report ports that look dead."""

    enabled: tuple[int, ...]
    sweeps: int = SWEEPS
    others_min: int = OTHERS_MIN
    dead: set[int] = field(default_factory=set)
    last_sweep: dict[int, int] = field(default_factory=dict)
    _history: deque[_Sweep] = field(init=False)
    #: Consecutive completed sweeps without a read, per port; any read from
    #: any window resets it.
    _streak: dict[int, int] = field(init=False)

    def __post_init__(self) -> None:
        """Size the history."""
        self.enabled = tuple(sorted(set(self.enabled)))
        self._history = deque(maxlen=self.sweeps)
        self._streak = dict.fromkeys(self.enabled, 0)

    def _counts(self, per_antenna: Mapping[int, int]) -> dict[int, int]:
        return {port: int(per_antenna.get(port, 0)) for port in self.enabled}

    def saw(self, per_antenna: Mapping[int, int]) -> set[int]:
        """Record reads from any window (sweep or continuous, even aborted).

        Returns the ports that were flagged and have now read again.
        """
        for port in self.enabled:
            if per_antenna.get(port, 0) > 0:
                self._streak[port] = 0
        revived = {p for p in self.dead if per_antenna.get(p, 0) > 0}
        self.dead -= revived
        return revived

    def sweep_completed(
        self, per_antenna: Mapping[int, int], present_at_start: int
    ) -> tuple[set[int], set[int]]:
        """Record a completed sweep; return ``(newly_dead, revived)``."""
        revived = self.saw(per_antenna)
        counts = self._counts(per_antenna)
        for port, n in counts.items():
            if n == 0:
                self._streak[port] += 1
        self.last_sweep = counts
        self._history.append(_Sweep(counts, present_at_start))
        newly = self._evaluate() - self.dead
        self.dead |= newly
        return newly, revived

    def _evaluate(self) -> set[int]:
        if len(self._history) < self.sweeps or not self.enabled:
            return set()
        history = list(self._history)
        silent = {p for p in self.enabled if self._streak[p] >= self.sweeps}
        if len(self.enabled) == 1:
            had_tags = any(s.present_at_start > 0 for s in history)
            return silent if had_tags else set()
        flagged = set()
        for port in silent:
            others = sum(n for s in history for p, n in s.counts.items() if p != port)
            if others >= self.others_min:
                flagged.add(port)
        return flagged


def message(ports: Iterable[int], sweeps: int = SWEEPS) -> str:
    """Notification text, one line per port."""
    return "\n".join(
        f"Antenna on port {port} gave no reads in {sweeps} sweeps — check its "
        "cable and connection."
        for port in sorted(ports)
    )
