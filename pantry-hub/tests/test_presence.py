from __future__ import annotations

from hypothesis import given, strategies as st
import pytest

from pantry_hub.presence import (
    Door,
    PresenceConfig,
    PresenceEvent,
    PresenceMachine,
    State,
)

CFG = PresenceConfig(arrive_reads=3, arrive_window_s=10, depart_after_s=900)


def kinds(events: list[PresenceEvent]) -> list[str]:
    return [e.kind for e in events]


def test_arrival_needs_n_reads_within_window() -> None:
    m = PresenceMachine(CFG)
    assert m.read("A", 0) == []
    assert m.state("A") is State.ARRIVING
    assert m.read("A", 4) == []
    ev = m.read("A", 9)
    assert kinds(ev) == ["arrived"]
    assert m.state("A") is State.PRESENT


def test_reads_spread_wider_than_window_do_not_arrive() -> None:
    m = PresenceMachine(CFG)
    for t in (0, 11, 22, 33):
        assert m.read("A", t) == []
    assert m.state("A") is State.ARRIVING


def test_arriving_expires_back_to_unknown() -> None:
    m = PresenceMachine(CFG)
    m.read("A", 0)
    m.read("A", 1)
    assert m.tick(20) == []
    assert m.state("A") is State.UNKNOWN


def test_single_read_threshold_arrives_at_once() -> None:
    m = PresenceMachine(
        PresenceConfig(arrive_reads=1, arrive_window_s=5, depart_after_s=60)
    )
    assert kinds(m.read("A", 0)) == ["arrived"]


def test_missing_then_departed_after_timeout() -> None:
    m = PresenceMachine(CFG)
    for t in (0, 1, 2):
        m.read("A", t)
    assert m.tick(63) == []
    assert m.state("A") is State.MISSING
    assert m.tick(2 + 899) == []
    ev = m.tick(2 + 900)
    assert kinds(ev) == ["departed"]
    assert m.state("A") is State.UNKNOWN


def test_read_while_missing_returns_to_present_without_event() -> None:
    m = PresenceMachine(CFG)
    for t in (0, 1, 2):
        m.read("A", t)
    m.tick(100)
    assert m.state("A") is State.MISSING
    assert m.read("A", 101) == []
    assert m.state("A") is State.PRESENT
    assert m.tick(101 + 899) == []


def test_departed_tag_returning_arrives_again() -> None:
    m = PresenceMachine(CFG)
    for t in (0, 1, 2):
        m.read("A", t)
    assert kinds(m.tick(1000)) == ["departed"]
    m.read("A", 1001)
    m.read("A", 1002)
    assert kinds(m.read("A", 1003)) == ["arrived"]


def test_restart_grace_window() -> None:
    m = PresenceMachine(CFG)
    m.restore(["A", "B"], now=5000)
    assert sorted(m.in_pantry()) == ["A", "B"]
    # A read of a restored tag is not a new arrival.
    assert m.read("A", 5001) == []
    assert m.tick(5000 + 899) == []
    ev = m.tick(5000 + 900)
    assert kinds(ev) == ["departed"]
    assert ev[0].epc == "B"


def test_door_gating_blocks_departure_until_closed_and_settled() -> None:
    cfg = PresenceConfig(
        arrive_reads=1,
        arrive_window_s=10,
        depart_after_s=100,
        depart_requires_door_closed=True,
    )
    m = PresenceMachine(cfg)
    m.read("A", 0)
    assert m.tick(200) == []  # door unknown → no departures
    m.set_door(Door.OPEN, 200)
    assert m.tick(300) == []
    m.set_door(Door.CLOSED, 300)
    assert m.tick(305) == []  # closed, but not settled for arrive_window_s
    assert kinds(m.tick(310)) == ["departed"]


def test_door_reopening_resets_settle_time() -> None:
    cfg = PresenceConfig(
        arrive_reads=1,
        arrive_window_s=10,
        depart_after_s=100,
        depart_requires_door_closed=True,
    )
    m = PresenceMachine(cfg)
    m.read("A", 0)
    m.set_door(Door.CLOSED, 100)
    m.set_door(Door.CLOSED, 105)  # repeated closed message keeps the original time
    m.set_door(Door.OPEN, 108)
    m.set_door(Door.CLOSED, 109)
    assert m.tick(115) == []
    assert kinds(m.tick(119)) == ["departed"]


def test_door_ignored_when_gating_off() -> None:
    m = PresenceMachine(
        PresenceConfig(arrive_reads=1, arrive_window_s=5, depart_after_s=60)
    )
    m.read("A", 0)
    m.set_door(Door.OPEN, 1)
    assert kinds(m.tick(61)) == ["departed"]


def test_invalid_config() -> None:
    with pytest.raises(ValueError):
        PresenceMachine(PresenceConfig(arrive_reads=0))
    with pytest.raises(ValueError):
        PresenceMachine(PresenceConfig(arrive_window_s=100, depart_after_s=100))


def test_out_of_order_timestamp_does_not_move_last_seen_back() -> None:
    m = PresenceMachine(
        PresenceConfig(arrive_reads=1, arrive_window_s=5, depart_after_s=60)
    )
    m.read("A", 100)
    m.read("A", 50)
    assert m.tracks()["A"].last_seen == 100


# Property tests --------------------------------------------------------------

timeline = st.lists(
    st.tuples(
        st.sampled_from(["read", "tick", "door_open", "door_closed", "restart"]),
        st.sampled_from(["A", "B", "C"]),
        st.floats(min_value=0.0, max_value=400.0, allow_nan=False),
    ),
    max_size=120,
)


@given(
    steps=timeline,
    arrive_reads=st.integers(1, 4),
    gated=st.booleans(),
)
def test_events_alternate_and_never_double_arrive(
    steps: list[tuple[str, str, float]], arrive_reads: int, gated: bool
) -> None:
    cfg = PresenceConfig(
        arrive_reads=arrive_reads,
        arrive_window_s=20,
        depart_after_s=300,
        depart_requires_door_closed=gated,
    )
    m = PresenceMachine(cfg)
    now = 0.0
    history: dict[str, list[str]] = {}
    for action, epc, dt in steps:
        now += dt
        events: list[PresenceEvent] = []
        if action == "read":
            events = m.read(epc, now)
        elif action == "tick":
            events = m.tick(now)
        elif action == "door_open":
            m.set_door(Door.OPEN, now)
        elif action == "door_closed":
            m.set_door(Door.CLOSED, now)
        else:  # restart: rebuild from what is in the pantry, as the hub does
            in_pantry = m.in_pantry()
            door = m.door
            m = PresenceMachine(cfg)
            m.restore(in_pantry, now)
            m.set_door(door, now)
        for e in events:
            history.setdefault(e.epc, []).append(e.kind)
            # A departed EPC is gone; an arrived one is in the pantry.
            assert (e.epc in m.in_pantry()) == (e.kind == "arrived")
    for seq in history.values():
        # arrived, departed, arrived, ... — never two arrivals in a row.
        for i, kind in enumerate(seq):
            assert kind == ("arrived" if i % 2 == 0 else "departed")
