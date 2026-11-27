from __future__ import annotations

import random

from pantry_hub.store import Link, Store, add_key, consume_key
from pantry_hub.worker import Backoff, GrocyWorker, op_note

from .memory_backend import MemoryBackend

T0 = 1_800_000_000.0


async def link(store: Store, epc: str, pid: int = 1, name: str = "Beans") -> None:
    created, _ = await store.create_link(
        Link(epc=epc, product_id=pid, product_name=name, linked_at=T0), location_id=1
    )
    assert created


async def drain(worker: GrocyWorker, limit: int = 50) -> list[float | None]:
    out: list[float | None] = []
    for _ in range(limit):
        res = await worker.process_one()
        out.append(res)
        if res == 3600.0:
            break
    return out


def worker_for(store: Store, backend: MemoryBackend) -> GrocyWorker:
    return GrocyWorker(store, backend, clock=lambda: T0, rng=random.Random(1))


async def test_arrival_of_linked_epc_journals_one_add(store: Store) -> None:
    await link(store, "AAAA")
    r1 = await store.apply_arrival("AAAA", T0, 1)
    assert r1.cycle == 1 and r1.add_op_id is not None
    op = await store.get_op(r1.add_op_id)
    assert op is not None and op.dedupe_key == add_key("AAAA", 1)


async def test_unlinked_arrival_goes_to_unlinked_list(store: Store) -> None:
    r = await store.apply_arrival("BBBB", T0, 1)
    assert r.link is None and r.add_op_id is None
    assert [u.epc for u in await store.unlinked()] == ["BBBB"]
    # Linking later removes it from the list and, since it is in the pantry,
    # journals the add for this arrival exactly once.
    created, op_id = await store.create_link(
        Link("BBBB", 2, "Rice", T0 + 1), location_id=1
    )
    assert created and op_id is not None
    assert await store.unlinked() == []
    again = await store.replace_link(Link("BBBB", 2, "Rice", T0 + 2), location_id=1)
    assert again is None  # same arrival: no second add


async def test_departure_consumes_only_when_add_journaled(store: Store) -> None:
    await store.apply_arrival("CCCC", T0, 1)  # unlinked → no add
    d = await store.apply_departure("CCCC", T0 + 10)
    assert d.consume_op_id is None
    await link(store, "DDDD")
    await store.apply_arrival("DDDD", T0, 1)
    d2 = await store.apply_departure("DDDD", T0 + 10)
    assert d2.consume_op_id is not None
    op = await store.get_op(d2.consume_op_id)
    assert op is not None and op.dedupe_key == consume_key("DDDD", 1)


async def test_worker_adds_then_consumes_the_same_unit(store: Store) -> None:
    backend = MemoryBackend()
    backend.add_product(1, "Beans")
    await link(store, "AAAA")
    await store.apply_arrival("AAAA", T0, 1)
    w = worker_for(store, backend)
    await drain(w)
    assert len(backend.stock) == 1
    sid = backend.stock[0].stock_id
    # Another unit of the same product added by hand must not be consumed.
    backend.stock.append(type(backend.stock[0])("manual", 1, 1.0, 1, ""))
    await store.apply_departure("AAAA", T0 + 1)
    await drain(w)
    assert ("consume", 1, sid) in backend.log
    assert [e.stock_id for e in backend.stock] == ["manual"]
    assert (await store.counts())["done"] == 2


async def test_queue_replays_in_order_after_outage(store: Store) -> None:
    backend = MemoryBackend()
    for pid, name, epc in [
        (1, "Beans", "AAAA"),
        (2, "Rice", "BBBB"),
        (3, "Oats", "CCCC"),
    ]:
        backend.add_product(pid, name)
        await link(store, epc, pid, name)
        await store.apply_arrival(epc, T0, 1)
    backend.faults.extend(["down", "down", "down"])
    w = worker_for(store, backend)
    delays = await drain(w)
    assert all(d is not None and d > 0 for d in delays[:3])  # three backoffs
    assert w.grocy_ok is True
    assert [entry[0:2] for entry in backend.log] == [("add", 1), ("add", 2), ("add", 3)]
    await store.apply_departure("AAAA", T0 + 1)
    await drain(w)
    assert backend.log[-1][0:2] == ("consume", 1)


async def test_lost_answer_does_not_double_add(store: Store) -> None:
    backend = MemoryBackend()
    backend.add_product(1, "Beans")
    await link(store, "AAAA")
    r = await store.apply_arrival("AAAA", T0, 1)
    backend.faults.append("lost")  # applied in Grocy, answer lost
    w = worker_for(store, backend)
    first = await w.process_one()
    assert first is not None and first > 0
    op = await store.get_op(r.add_op_id or 0)
    assert op is not None and op.maybe_sent and op.status == "pending"
    await drain(w)
    assert len(backend.stock) == 1
    assert backend.stock[0].note == op_note(op)
    done = await store.get_op(op.id)
    assert done is not None and done.status == "done"
    assert done.stock_entry_id == backend.stock[0].stock_id


async def test_lost_consume_answer_does_not_double_consume(store: Store) -> None:
    backend = MemoryBackend()
    backend.add_product(1, "Beans")
    await link(store, "AAAA")
    await store.apply_arrival("AAAA", T0, 1)
    w = worker_for(store, backend)
    await drain(w)
    backend.stock.append(type(backend.stock[0])("other", 1, 1.0, 1, ""))
    await store.apply_departure("AAAA", T0 + 1)
    backend.faults.append("lost")
    await drain(w)
    assert [e.stock_id for e in backend.stock] == ["other"]


async def test_ambiguous_then_down_keeps_checking(store: Store) -> None:
    backend = MemoryBackend()
    backend.add_product(1, "Beans")
    await link(store, "AAAA")
    r = await store.apply_arrival("AAAA", T0, 1)
    backend.faults.extend(["lost", "down"])  # lost add, then the check fails
    w = worker_for(store, backend)
    await w.process_one()
    await w.process_one()
    op = await store.get_op(r.add_op_id or 0)
    assert op is not None and op.maybe_sent  # still ambiguous
    await drain(w)
    assert len(backend.stock) == 1


async def test_rejected_op_fails_without_blocking_queue(store: Store) -> None:
    backend = MemoryBackend()
    backend.add_product(1, "Beans")
    backend.add_product(2, "Rice")
    await link(store, "AAAA", 1)
    await link(store, "BBBB", 2, "Rice")
    await store.apply_arrival("AAAA", T0, 1)
    await store.apply_arrival("BBBB", T0, 1)
    backend.faults.append("reject")
    w = worker_for(store, backend)
    await drain(w)
    counts = await store.counts()
    assert (counts["pending"], counts["done"], counts["failed"]) == (0, 1, 1)
    failed = await store.ops("failed")
    assert failed[0].epc == "AAAA" and "rejected" in (failed[0].last_error or "")
    # Retry while the item is still in the pantry: the add is replayed.
    assert await store.retry_failed(failed[0].id)
    assert not await store.retry_failed(failed[0].id)
    await drain(w)
    assert (await store.counts())["failed"] == 0
    assert [e.product_id for e in backend.stock] == [2, 1]


async def test_q3_failed_add_retried_after_item_left_is_cancelled(store: Store) -> None:
    """Q3 regression: a failed add retried after its arrival departed must not
    reach Grocy (it would never be consumed)."""
    backend = MemoryBackend()
    backend.add_product(1, "Beans")
    await link(store, "AAAA", 1)
    await store.apply_arrival("AAAA", T0, 1)
    backend.faults.append("reject")
    w = worker_for(store, backend)
    await drain(w)
    failed = await store.ops("failed")
    assert len(failed) == 1
    d = await store.apply_departure("AAAA", T0 + 1)  # failed add → no consume
    assert d.consume_op_id is None
    assert await store.retry_failed(failed[0].id)  # retry from the web UI
    await drain(w)
    op = await store.get_op(failed[0].id)
    assert op is not None and (op.status, op.resolution) == ("done", "cancelled")
    assert backend.log == []  # nothing sent to Grocy
    # The item coming back is a new arrival and is added normally.
    await store.apply_arrival("AAAA", T0 + 2, 1)
    await drain(w)
    assert [entry[0] for entry in backend.log] == ["add"]


async def test_q3_add_and_consume_cancelled_when_item_left_while_grocy_down(
    store: Store,
) -> None:
    """Q3: arrival + departure both queued while Grocy is down → net zero, no calls."""
    backend = MemoryBackend()
    backend.add_product(1, "Beans")
    await link(store, "AAAA", 1)
    r = await store.apply_arrival("AAAA", T0, 1)
    d = await store.apply_departure("AAAA", T0 + 1)  # consume journaled behind add
    assert d.consume_op_id is not None
    w = worker_for(store, backend)
    await drain(w)
    add = await store.get_op(r.add_op_id or 0)
    consume = await store.get_op(d.consume_op_id)
    assert add is not None and consume is not None
    assert (add.status, add.resolution) == ("done", "cancelled")
    assert (consume.status, consume.resolution) == ("done", "cancelled")
    assert backend.log == []
    assert (await store.counts())["pending"] == 0


async def test_q3_ambiguous_add_that_landed_is_kept_and_consumed(store: Store) -> None:
    """Q3 + idempotency: if the add did reach Grocy before the item left, keep
    it and let the queued consume remove it (not a cancel)."""
    backend = MemoryBackend()
    backend.add_product(1, "Beans")
    await link(store, "AAAA", 1)
    await store.apply_arrival("AAAA", T0, 1)
    backend.faults.append("lost")  # applied in Grocy, answer lost
    w = worker_for(store, backend)
    await w.process_one()
    await store.apply_departure("AAAA", T0 + 1)
    await drain(w)
    assert backend.stock == []
    assert [entry[0] for entry in backend.log] == ["add", "consume"]


async def test_q3_ambiguous_add_not_found_cancels_and_verifies(store: Store) -> None:
    """Q3: an ambiguous add of a departed cycle that is not in Grocy yet is
    cancelled, and the delayed check removes it if it lands late."""
    backend = MemoryBackend()
    backend.add_product(1, "Beans")
    await link(store, "AAAA", 1)
    r = await store.apply_arrival("AAAA", T0, 1)
    now = [0.0]
    w = GrocyWorker(
        store,
        backend,
        clock=lambda: T0,
        rng=random.Random(1),
        verify_after=10,
        verify_clock=lambda: now[0],
    )
    backend.faults.append("late")  # times out unapplied; lands two calls later
    await w.process_one()
    await store.apply_departure("AAAA", T0 + 1)
    await w.process_one()  # entries check (not there yet) → cancel + verify
    op = await store.get_op(r.add_op_id or 0)
    assert op is not None and op.resolution == "cancelled"
    await backend.entries(1)  # time passes: the late add lands
    assert len(backend.stock) == 1
    now[0] = 11
    assert await w.process_one() is None  # duplicate check: keep nothing
    assert backend.stock == []
    assert w.duplicates_removed == 1


async def test_consume_of_failed_add_fails(store: Store) -> None:
    """A consume whose add failed (e.g. rejected after the consume was
    journaled) is failed too, never sent."""
    backend = MemoryBackend()
    backend.add_product(1, "Beans")
    await link(store, "AAAA", 1)
    r = await store.apply_arrival("AAAA", T0, 1)
    d = await store.apply_departure("AAAA", T0 + 1)
    assert d.consume_op_id is not None
    await store.mark_failed(r.add_op_id or 0, "rejected", T0)
    w = worker_for(store, backend)
    await drain(w)
    consume = await store.get_op(d.consume_op_id)
    assert consume is not None and consume.status == "failed"
    assert "nothing to consume" in (consume.last_error or "")
    assert backend.log == []


async def test_auth_error_keeps_op_queued(store: Store) -> None:
    backend = MemoryBackend()
    backend.add_product(1, "Beans")
    await link(store, "AAAA")
    await store.apply_arrival("AAAA", T0, 1)
    backend.faults.append("auth")
    w = worker_for(store, backend)
    delay = await w.process_one()
    assert delay is not None and delay > 0
    assert w.grocy_ok is False
    assert (await store.counts())["pending"] == 1


def test_backoff_grows_and_caps() -> None:
    b = Backoff(base=1, cap=10)
    rng = random.Random(0)
    delays = [b.next_delay(rng) for _ in range(8)]
    assert all(0.5 <= d <= 10 for d in delays)
    assert delays[-1] >= 5  # capped at 10 with jitter 0.5..1
    b.reset()
    assert b.next_delay(rng) <= 1


async def test_worker_task_runs_and_stops(store: Store) -> None:
    from .conftest import eventually

    backend = MemoryBackend()
    backend.add_product(1, "Beans")
    w = worker_for(store, backend)
    w.backoff = Backoff(base=0.01, cap=0.05)
    w.start()
    await link(store, "AAAA")
    await store.apply_arrival("AAAA", T0, 1)
    backend.faults.extend(["down", "down"])
    w.wake()
    await eventually(lambda: len(backend.stock) == 1)
    await w.stop()
    await w.stop()  # idempotent


async def test_close_after_writes_and_checkpoint_failure(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Regression: unclosed write cursors made close() fail ("table is locked")
    and left aiosqlite's thread running, hanging shutdown."""
    s = await Store.open(tmp_path / "c.db")
    await link(s, "AAAA")
    await s.apply_arrival("AAAA", T0, 1)
    await s.touch({"AAAA": T0 + 1})
    op = await s.next_pending()
    assert op is not None
    await s.mark_sending(op.id)
    await s.mark_retry(op.id, "x", maybe_sent=True)
    await s.schedule_verify(op.id, T0)
    await s.mark_done(op.id, T0, "s1")
    await s.close()  # must checkpoint cleanly
    s2 = await Store.open(tmp_path / "c.db")
    assert (await s2.counts())["done"] == 1
    await s2._conn.execute("SELECT 1")  # leave a cursor open on purpose
    held = await s2._conn.execute("SELECT * FROM ops")
    await held.fetchone()
    await s2.close()  # still closes even if the checkpoint is refused


async def test_verify_removes_late_duplicates(store: Store) -> None:
    backend = MemoryBackend()
    backend.add_product(1, "Beans")
    await link(store, "AAAA")
    r = await store.apply_arrival("AAAA", T0, 1)
    now = [0.0]
    w = GrocyWorker(
        store,
        backend,
        clock=lambda: T0,
        rng=random.Random(1),
        verify_after=10,
        verify_clock=lambda: now[0],
    )
    backend.faults.append("lost")
    await w.process_one()  # applied, answer lost → ambiguous
    await w.process_one()  # check finds it → done, verify scheduled
    op = await store.get_op(r.add_op_id or 0)
    assert op is not None and op.status == "done"
    assert (await store.counts())["verifying"] == 1
    # The timed-out first request lands late in Grocy: a duplicate entry.
    from pantry_hub.grocy import StockEntry
    from pantry_hub.worker import op_note

    backend.stock.append(StockEntry("late", 1, 1.0, 1, op_note(op)))
    assert 9 <= (await w.process_one() or 0) <= 10  # not due yet
    now[0] = 11
    backend.faults.append("down")
    assert (await w.process_one() or 0) > 0  # check fails → rescheduled
    now[0] = 400
    assert await w.process_one() is None
    assert [e.stock_id for e in backend.stock] == [op.stock_entry_id]
    assert w.duplicates_removed == 1
    assert (await store.counts())["verifying"] == 0
    assert await w.process_one() == 3600.0
