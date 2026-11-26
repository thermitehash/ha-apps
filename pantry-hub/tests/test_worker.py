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
    backend.add_product(1, "Beans")
    backend.add_product(2, "Rice")
    await link(store, "AAAA", 1, "Beans")
    await link(store, "BBBB", 2, "Rice")
    await store.apply_arrival("AAAA", T0, 1)
    await store.apply_arrival("BBBB", T0, 1)
    await store.apply_departure("AAAA", T0 + 1)
    backend.faults.extend(["down", "down", "down"])
    w = worker_for(store, backend)
    delays = await drain(w)
    assert delays[:3] == [d for d in delays[:3] if d is not None and d > 0]
    assert w.grocy_ok is True
    assert [entry[0:2] for entry in backend.log] == [
        ("add", 1),
        ("add", 2),
        ("consume", 1),
    ]


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
    assert counts == {"pending": 0, "done": 1, "failed": 1}
    failed = await store.ops("failed")
    assert failed[0].epc == "AAAA" and "rejected" in (failed[0].last_error or "")
    # Departure of the failed add does not journal a consume.
    d = await store.apply_departure("AAAA", T0 + 1)
    assert d.consume_op_id is None
    # Retry from the UI.
    assert await store.retry_failed(failed[0].id)
    assert not await store.retry_failed(failed[0].id)
    await drain(w)
    assert (await store.counts())["failed"] == 0


async def test_consume_of_failed_add_fails(store: Store) -> None:
    backend = MemoryBackend()
    backend.add_product(1, "Beans")
    await link(store, "AAAA", 1)
    await store.apply_arrival("AAAA", T0, 1)
    d = await store.apply_departure("AAAA", T0 + 1)  # consume journaled behind add
    assert d.consume_op_id is not None
    backend.faults.append("reject")
    w = worker_for(store, backend)
    await drain(w)
    consume = await store.get_op(d.consume_op_id)
    assert consume is not None and consume.status == "failed"
    assert "nothing to consume" in (consume.last_error or "")


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
