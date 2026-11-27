"""Presence in door mode: missed sweeps, aborted sweeps, restart rule, timer."""

from __future__ import annotations

from hypothesis import given, strategies as st

from pantry_hub.presence import PresenceConfig, PresenceEvent, PresenceMachine, State

DOOR_MODE = PresenceConfig(
    arrive_reads=3,
    arrive_window_s=10,
    depart_after_s=900,
    depart_after_missed_sweeps=2,
    min_sweeps_before_departure=2,
)


def kinds(events: list[PresenceEvent]) -> list[str]:
    return [e.kind for e in events]


def arrive(m: PresenceMachine, epc: str, t: float) -> None:
    for i in range(3):
        m.read(epc, t + i)
    assert m.state(epc) is State.PRESENT


def sweep(
    m: PresenceMachine, t: float, seen: list[str], completed: bool = True
) -> list[PresenceEvent]:
    m.start_sweep()
    for epc in seen:
        m.read(epc, t)
    return m.finish_sweep(completed, t + 45)


def ready(m: PresenceMachine) -> None:
    """Satisfy the restart rule (2 completed sweeps)."""
    sweep(m, -200, list(m.tracks()))
    sweep(m, -100, list(m.tracks()))


def test_arrival_rule_applies_within_any_window() -> None:
    m = PresenceMachine(DOOR_MODE)
    m.start_sweep()
    m.read("A", 0)
    m.read("A", 1)
    assert kinds(m.read("A", 2)) == ["arrived"]
    m.finish_sweep(True, 45)


def test_departs_after_two_consecutive_missed_completed_sweeps() -> None:
    m = PresenceMachine(DOOR_MODE)
    arrive(m, "A", 0)
    ready(m)
    assert sweep(m, 100, []) == []
    assert m.state("A") is State.MISSING and m.tracks()["A"].missed == 1
    assert kinds(sweep(m, 200, [])) == ["departed"]
    assert m.state("A") is State.UNKNOWN


def test_a_read_between_sweeps_resets_the_miss_count() -> None:
    m = PresenceMachine(DOOR_MODE)
    arrive(m, "A", 0)
    ready(m)
    sweep(m, 100, [])
    m.read("A", 150)  # e.g. during a continuous window
    assert m.state("A") is State.PRESENT and m.tracks()["A"].missed == 0
    assert sweep(m, 200, []) == []
    assert kinds(sweep(m, 300, [])) == ["departed"]


def test_seen_in_sweep_resets_misses() -> None:
    m = PresenceMachine(DOOR_MODE)
    arrive(m, "A", 0)
    ready(m)
    sweep(m, 100, [])
    sweep(m, 200, ["A"])
    assert m.tracks()["A"].missed == 0
    assert sweep(m, 300, []) == []


def test_aborted_sweeps_never_count_as_misses() -> None:
    m = PresenceMachine(DOOR_MODE)
    arrive(m, "A", 0)
    ready(m)
    for t in range(10):
        assert sweep(m, 100 + t * 100, [], completed=False) == []
    assert m.tracks()["A"].missed == 0
    assert m.sweeps_aborted == 10
    sweep(m, 2000, [])
    assert sweep(m, 2100, [], completed=False) == []
    assert m.tracks()["A"].missed == 1  # the aborted one in between changed nothing
    assert kinds(sweep(m, 2200, [])) == ["departed"]


def test_restart_no_departures_until_two_completed_sweeps() -> None:
    m = PresenceMachine(DOOR_MODE)
    m.restore(["A", "B"], 0)
    assert not m.departures_allowed
    sweep(m, 0, [])  # startup sweep: A and B both missing
    assert m.in_pantry() == ["A", "B"]  # first completed sweep: nothing departs
    sweep(m, 100, [], completed=False)  # aborted: still only 1 completed
    assert not m.departures_allowed
    events = sweep(m, 200, ["B"])  # second completed sweep
    assert m.sweeps_completed == 2
    assert kinds(events) == ["departed"] and events[0].epc == "A"


def test_restart_rule_also_blocks_timer_departures() -> None:
    m = PresenceMachine(DOOR_MODE)
    m.restore(["A"], 0)
    assert m.tick(5000, accrue=True) == []  # unseen ≥ 900 s, but 0 sweeps done
    sweep(m, 5000, ["A"])
    sweep(m, 5100, ["A"])
    m.tick(5200, accrue=False)
    assert kinds(m.tick(6200, accrue=True)) == ["departed"]


def test_unseen_timer_suspended_while_not_reading() -> None:
    m = PresenceMachine(DOOR_MODE)
    arrive(m, "A", 0)
    ready(m)
    # 10 hours of idle (door closed, no continuous window): timer frozen.
    assert m.tick(36_000, accrue=False) == []
    assert m.tracks()["A"].unseen == 0
    assert m.state("A") is State.PRESENT
    # Continuous window: the timer runs again.
    assert m.tick(36_500, accrue=True) == []
    assert m.state("A") is State.MISSING  # > missing_after_s of reading time
    assert m.tick(36_600, accrue=False) == []
    assert kinds(m.tick(37_000, accrue=True)) == ["departed"]  # 500 + 400 s


def test_timer_counts_only_reading_time_across_windows() -> None:
    m = PresenceMachine(DOOR_MODE)
    arrive(m, "A", 0)
    ready(m)
    t = 2.0
    for _ in range(8):  # 8 × 100 s of continuous reading with idle gaps
        t += 1000
        m.tick(t, accrue=False)
        t += 100
        assert m.tick(t, accrue=True) == []
    assert m.tracks()["A"].unseen == 800
    t += 100
    assert kinds(m.tick(t, accrue=True)) == ["departed"]


def test_missed_sweeps_disabled_in_continuous_mode() -> None:
    m = PresenceMachine(
        PresenceConfig(arrive_reads=1, arrive_window_s=5, depart_after_s=60)
    )
    m.read("A", 0)
    for t in range(5):
        assert sweep(m, 10 + t, []) == []


# Property ----------------------------------------------------------------------

steps = st.lists(
    st.tuples(
        st.sampled_from(
            ["read", "idle", "continuous", "sweep_ok", "sweep_abort", "restart"]
        ),
        st.sampled_from(["A", "B"]),
        st.floats(0, 1500, allow_nan=False),
        st.booleans(),
    ),
    max_size=80,
)


@given(steps)
def test_door_mode_invariants(script: list[tuple[str, str, float, bool]]) -> None:
    """Random door-mode timelines (incl. restarts, aborted sweeps):
    events alternate; nothing departs before 2 completed sweeps since the last
    restart; aborted sweeps and idle time never cause departures; a tag read in
    a completed sweep never departs at that sweep."""
    m = PresenceMachine(DOOR_MODE)
    now = 0.0
    history: dict[str, list[str]] = {}
    for action, epc, dt, flag in script:
        now += dt
        events: list[PresenceEvent] = []
        if action == "read":
            events = m.read(epc, now)
        elif action == "idle":
            events = m.tick(now, accrue=False)
            assert events == []
        elif action == "continuous":
            events = m.tick(now, accrue=True)
        elif action in ("sweep_ok", "sweep_abort"):
            m.start_sweep()
            read_now = flag
            if read_now:
                m.read(epc, now)
            events = m.finish_sweep(action == "sweep_ok", now)
            if action == "sweep_abort":
                assert [e for e in events if e.kind == "departed"] == []
            if read_now:
                assert epc not in [e.epc for e in events if e.kind == "departed"]
        else:
            in_pantry = m.in_pantry()
            m = PresenceMachine(DOOR_MODE)
            m.restore(in_pantry, now)
        departed = [e for e in events if e.kind == "departed"]
        if departed:
            assert m.sweeps_completed >= 2
        for e in events:
            history.setdefault(e.epc, []).append(e.kind)
    for seq in history.values():
        for i, kind in enumerate(seq):
            assert kind == ("arrived" if i % 2 == 0 else "departed")
