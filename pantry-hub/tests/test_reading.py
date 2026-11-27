"""Reading planner: every state transition of door-driven reading."""

from __future__ import annotations

from hypothesis import given, strategies as st
import pytest

from pantry_hub.presence import Door
from pantry_hub.reading import Activity, ReadingConfig, ReadingPlanner, SweepReason

CFG = ReadingConfig(
    open_continuous_s=600,
    open_sweep_interval_s=300,
    sweep_s=45,
    closed_sweep_interval_s=3600,
    retry_s=60,
)


def planner(now: float = 0.0, door: Door = Door.CLOSED) -> ReadingPlanner:
    p = ReadingPlanner(CFG, now)
    p.set_door(door, now)
    return p


def after_startup(now: float = 0.0, door: Door = Door.CLOSED) -> ReadingPlanner:
    p = planner(now, door)
    assert p.next(now).reason is SweepReason.STARTUP
    p.sweep_done(True, now + 45)
    return p


def test_startup_runs_one_sweep_first() -> None:
    p = ReadingPlanner(CFG, 0)
    plan = p.next(0)
    assert (plan.activity, plan.duration, plan.reason) == (
        Activity.SWEEP,
        45,
        SweepReason.STARTUP,
    )
    # The initial door report (unknown → closed) doesn't abort the startup sweep.
    assert p.set_door(Door.CLOSED, 1) is False
    assert p.next(1).reason is SweepReason.STARTUP


def test_startup_while_door_open_sweeps_then_reads_continuously() -> None:
    p = ReadingPlanner(CFG, 0)
    assert p.set_door(Door.OPEN, 0) is True  # opening aborts whatever runs
    assert p.next(0).reason is SweepReason.STARTUP
    p.sweep_done(True, 45)
    plan = p.next(45)
    assert plan.activity is Activity.CONTINUOUS and plan.duration == pytest.approx(555)


def test_closed_sweeps_every_closed_interval() -> None:
    p = after_startup()
    plan = p.next(100)
    assert plan.activity is Activity.IDLE and plan.duration == pytest.approx(3545)
    plan = p.next(3645)
    assert (plan.activity, plan.reason) == (Activity.SWEEP, SweepReason.SCHEDULED)
    p.sweep_done(True, 3690)
    assert p.next(3700).duration == pytest.approx(3590)


def test_door_open_continuous_then_sweeps_every_open_interval() -> None:
    p = after_startup()
    assert p.set_door(Door.OPEN, 1000) is True
    plan = p.next(1000)
    assert (plan.activity, plan.duration) == (Activity.CONTINUOUS, 600)
    plan = p.next(1300)  # e.g. the loop asked again mid-window
    assert plan.activity is Activity.CONTINUOUS and plan.duration == 300
    p.continuous_done()
    plan = p.next(1600)
    assert (plan.activity, plan.reason) == (Activity.SWEEP, SweepReason.SCHEDULED)
    p.sweep_done(True, 1645)
    plan = p.next(1650)
    assert plan.activity is Activity.IDLE and plan.duration == pytest.approx(295)
    assert p.next(1945).activity is Activity.SWEEP


def test_continuous_window_end_by_clock_even_without_done() -> None:
    p = after_startup()
    p.set_door(Door.OPEN, 1000)
    assert p.next(1600).activity is Activity.SWEEP


def test_door_close_sweeps_immediately_then_closed_schedule() -> None:
    p = after_startup()
    p.set_door(Door.OPEN, 1000)
    assert p.set_door(Door.CLOSED, 1100) is True  # stop the continuous window
    plan = p.next(1100)
    assert (plan.activity, plan.duration, plan.reason) == (
        Activity.SWEEP,
        45,
        SweepReason.CLOSE,
    )
    p.sweep_done(True, 1145)
    plan = p.next(1150)
    assert plan.activity is Activity.IDLE and plan.duration == pytest.approx(3595)


def test_close_sweep_retried_if_it_fails() -> None:
    p = after_startup()
    p.set_door(Door.OPEN, 1000)
    p.set_door(Door.CLOSED, 1100)
    p.next(1100)
    p.sweep_done(False, 1110)  # reader dropped
    plan = p.next(1111)
    assert plan.activity is Activity.IDLE and plan.duration == pytest.approx(59)
    assert p.next(1170).reason is SweepReason.CLOSE  # still pending, retried
    p.sweep_done(True, 1215)
    assert p.next(1215).activity is Activity.IDLE


def test_failed_startup_sweep_keeps_pending_close_sweep() -> None:
    """Regression (found by the property test): a failed startup sweep used to
    clear the close sweep queued behind it."""
    p = ReadingPlanner(CFG, 0)
    p.set_door(Door.OPEN, 0)
    p.set_door(Door.CLOSED, 0)
    p.sweep_done(False, 0)  # the startup sweep failed
    assert p.next(1).activity is Activity.IDLE  # retry_s, not a tight loop
    assert p.next(60).reason is SweepReason.CLOSE


def test_failed_scheduled_sweep_retries_after_retry_s() -> None:
    p = after_startup()
    p.next(3645)
    p.sweep_done(False, 3650)
    plan = p.next(3651)
    assert plan.activity is Activity.IDLE and plan.duration == pytest.approx(59)
    plan = p.next(3710)
    assert (plan.activity, plan.reason) == (Activity.SWEEP, SweepReason.RETRY)


def test_failed_startup_sweep_retries() -> None:
    p = planner()
    p.sweep_done(False, 5)
    assert p.next(10).activity is Activity.IDLE
    assert p.next(65).reason is SweepReason.RETRY


def test_unavailable_door_behaves_like_closed_never_open() -> None:
    p = after_startup()
    p.set_door(Door.OPEN, 1000)
    assert p.next(1000).activity is Activity.CONTINUOUS
    assert p.set_door(Door.UNKNOWN, 1010) is True  # stop reading continuously
    plan = p.next(1010)
    assert plan.activity is Activity.IDLE  # closed schedule, not continuous
    assert p.next(3645).activity is Activity.SWEEP
    # Closed ↔ unknown changes don't abort a sweep.
    assert p.set_door(Door.CLOSED, 3646) is False
    assert p.set_door(Door.UNKNOWN, 3647) is False


def test_sweep_now() -> None:
    p = after_startup()
    p.request_sweep()
    p.request_sweep()  # idempotent
    plan = p.next(100)
    assert (plan.activity, plan.reason) == (Activity.SWEEP, SweepReason.REQUESTED)
    p.sweep_done(True, 145)
    assert p.next(150).activity is Activity.IDLE


def test_sweep_now_while_open_resumes_continuous() -> None:
    p = after_startup()
    p.set_door(Door.OPEN, 1000)
    p.request_sweep()
    assert p.next(1100).reason is SweepReason.REQUESTED
    p.sweep_done(True, 1145)
    plan = p.next(1145)
    assert plan.activity is Activity.CONTINUOUS and plan.duration == pytest.approx(455)


def test_reopening_restarts_the_continuous_window() -> None:
    p = after_startup()
    p.set_door(Door.OPEN, 1000)
    p.continuous_done()
    p.set_door(Door.CLOSED, 1700)
    p.next(1700)
    p.sweep_done(True, 1745)
    p.set_door(Door.OPEN, 1800)
    plan = p.next(1800)
    assert (plan.activity, plan.duration) == (Activity.CONTINUOUS, 600)


def test_repeated_door_report_is_not_a_change() -> None:
    p = after_startup()
    p.set_door(Door.OPEN, 1000)
    assert p.set_door(Door.OPEN, 1001) is False
    assert p.next(1001).duration == pytest.approx(599)  # window not restarted


def test_invalid_config() -> None:
    with pytest.raises(ValueError):
        ReadingPlanner(ReadingConfig(sweep_s=400), 0)
    with pytest.raises(ValueError):
        ReadingPlanner(ReadingConfig(sweep_s=0), 0)


@given(
    st.lists(
        st.tuples(
            st.sampled_from(
                ["open", "close", "unavailable", "done_ok", "done_fail", "now", "wait"]
            ),
            st.floats(0, 4000, allow_nan=False),
        ),
        max_size=80,
    )
)
def test_flapping_never_reads_continuously_unless_open(
    steps: list[tuple[str, float]],
) -> None:
    """Door flapping / sensor drops: continuous reading only while open,
    plans are always well-formed, and a close always leads to a sweep."""
    p = ReadingPlanner(CFG, 0)
    now = 0.0
    close_pending = False
    for action, dt in steps:
        now += dt
        if action == "open":
            p.set_door(Door.OPEN, now)
            close_pending = False
        elif action == "close":
            if p.door is Door.OPEN:
                close_pending = True
            p.set_door(Door.CLOSED, now)
        elif action == "unavailable":
            p.set_door(Door.UNKNOWN, now)
        elif action == "done_ok":
            if p.next(now).activity is Activity.SWEEP:
                p.sweep_done(True, now)
                close_pending = False
        elif action == "done_fail":
            if p.next(now).activity is Activity.SWEEP:
                p.sweep_done(False, now)
        elif action == "now":
            p.request_sweep()
        plan = p.next(now)
        assert plan.duration >= 0
        if plan.activity is Activity.CONTINUOUS:
            assert p.door is Door.OPEN
        if close_pending and p.door is Door.CLOSED:
            # The close sweep runs now, or after retry_s if the last one failed.
            assert plan.activity is Activity.SWEEP or (
                plan.activity is Activity.IDLE and plan.duration <= CFG.retry_s
            )
