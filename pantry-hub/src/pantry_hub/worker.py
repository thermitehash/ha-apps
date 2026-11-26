"""Replay the Grocy journal in order, with retries, backoff and idempotency.

Rules:

* Strict FIFO: the oldest pending op blocks the ones behind it, so Grocy
  sees adds and consumes in the order they happened.
* Before a request is sent the op is marked ``maybe_sent``. If the request
  then fails in a way that may have reached Grocy (timeout, 5xx, connection
  reset), the retry first checks Grocy:
  - add: is there a stock entry whose note carries this op's marker?
  - consume: is the unit (``stock_entry_id``) gone?
  Only if not is the request sent again. This is what makes an arrival add
  at most one unit even across timeouts and restarts.
* A pre-retry check cannot see a request Grocy is *still* processing: a
  timed-out add can land after its retry. So every add that may have been
  sent more than once gets a delayed duplicate check (``verify_after``,
  persisted): stock entries carrying the same op marker beyond the one the
  hub tracks are consumed again, by stock entry id, and logged.
* Grocy rejecting a request (4xx) marks the op ``failed``. Failed ops stay in
  the journal, are counted in ``sensor.pantry_hub_grocy_queue`` and are
  shown in the web UI with a retry button. Nothing is dropped silently.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import contextlib
from dataclasses import dataclass
import logging
import random
import time

from .grocy import (
    GrocyAuthError,
    GrocyError,
    GrocyRequestError,
    GrocyUnavailable,
    StockBackend,
)
from .store import Op, Store

_LOGGER = logging.getLogger(__name__)


def op_note(op: Op) -> str:
    """Note stored on the Grocy stock entry; also the idempotency marker."""
    return f"Pantry Hub op {op.id} EPC {op.epc}"


@dataclass(slots=True)
class Backoff:
    """Exponential backoff with full jitter."""

    base: float = 1.0
    cap: float = 300.0
    failures: int = 0

    def next_delay(self, rng: random.Random) -> float:
        """Delay before the next attempt; counts one more failure."""
        delay = min(self.cap, self.base * (2**self.failures))
        self.failures = min(self.failures + 1, 30)
        return float(delay * rng.uniform(0.5, 1.0))

    def reset(self) -> None:
        """Forget failures after a success."""
        self.failures = 0


class GrocyWorker:
    """Background task that drains the journal into Grocy."""

    def __init__(
        self,
        store: Store,
        backend: StockBackend,
        *,
        clock: Callable[[], float] = time.time,
        backoff: Backoff | None = None,
        rng: random.Random | None = None,
        on_change: Callable[[], None] | None = None,
        on_done: Callable[[Op], Awaitable[None]] | None = None,
        verify_after: float = 60.0,
        verify_clock: Callable[[], float] = time.time,
    ) -> None:
        """Create; call :meth:`start`."""
        self.store = store
        self.backend = backend
        self.clock = clock
        self.backoff = backoff or Backoff()
        self.rng = rng or random.Random()
        self.on_change = on_change
        self.on_done = on_done
        self.verify_after = verify_after
        self.verify_clock = verify_clock
        #: Late duplicate adds found and consumed again.
        self.duplicates_removed = 0
        #: None = no request made yet; True = last request answered.
        self.grocy_ok: bool | None = None
        self.last_error: str | None = None
        self._wake = asyncio.Event()
        self._stopping = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._in_flight = False

    @property
    def in_flight(self) -> bool:
        """Whether a Grocy request is in progress."""
        return self._in_flight

    def wake(self) -> None:
        """Tell the worker new ops were journaled (or to retry now)."""
        self._wake.set()

    def start(self) -> None:
        """Start the background task."""
        if self._task is None:
            self._task = asyncio.create_task(
                self._run(), name="pantry_hub.grocy_worker"
            )

    async def stop(self, timeout: float = 20.0) -> None:
        """Finish the in-flight request (up to ``timeout``), then stop."""
        self._stopping.set()
        self._wake.set()
        task, self._task = self._task, None
        if task is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout)
        except TimeoutError:
            _LOGGER.warning(
                "Grocy request still running after %.0fs; cancelling", timeout
            )
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                delay = await self.process_one()
            except Exception:
                _LOGGER.exception("Unexpected error in the Grocy worker; retrying")
                delay = self.backoff.next_delay(self.rng)
            if self._stopping.is_set():
                break
            if delay is None:
                continue  # progressed; take the next op at once
            self._wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), delay)

    async def process_one(self) -> float | None:
        """Try the oldest pending op.

        Returns None when an op finished (call again at once), otherwise the
        seconds to wait: a backoff delay after a failure, or a long idle wait
        when the queue is empty (new ops call :meth:`wake`).
        """
        due = await self.store.next_verify()
        if due is not None and due[1] <= self.verify_clock():
            return await self._verify(due[0])
        op = await self.store.next_pending()
        if op is None:
            if due is None:
                return 3600.0
            return max(0.05, due[1] - self.verify_clock())
        ambiguous = op.maybe_sent
        self._in_flight = True
        try:
            if op.kind == "add":
                ambiguous = await self._do_add(op, ambiguous)
            else:
                ambiguous = await self._do_consume(op, ambiguous)
        except GrocyUnavailable as err:
            return await self._transient(op, str(err), ambiguous or err.maybe_applied)
        except GrocyAuthError as err:
            self.grocy_ok = False
            _LOGGER.error(
                "%s. Fix grocy_api_key in the app options; op %d stays queued",
                err,
                op.id,
            )
            return await self._transient(op, str(err), ambiguous)
        except GrocyRequestError as err:
            self.grocy_ok = True
            await self._fail(op, f"Grocy rejected {op.kind}: {err}")
            return None
        except GrocyError as err:
            return await self._transient(op, str(err), ambiguous)
        finally:
            self._in_flight = False
        return None

    async def _transient(self, op: Op, error: str, maybe_sent: bool) -> float:
        self.grocy_ok = False
        self.last_error = error
        await self.store.mark_retry(op.id, error, maybe_sent=maybe_sent)
        delay = self.backoff.next_delay(self.rng)
        _LOGGER.warning(
            "Grocy %s for op %d (%s) failed: %s; retrying in %.0fs",
            op.kind,
            op.id,
            op.epc,
            error,
            delay,
        )
        self._changed()
        return delay

    async def _fail(self, op: Op, error: str) -> None:
        self.last_error = error
        await self.store.mark_failed(op.id, error, self.clock())
        _LOGGER.error(
            "Op %d (%s %s) failed permanently: %s", op.id, op.kind, op.epc, error
        )
        self._changed()

    async def _done(self, op: Op, stock_entry_id: str | None = None) -> None:
        self.grocy_ok = True
        self.last_error = None
        self.backoff.reset()
        await self.store.mark_done(op.id, self.clock(), stock_entry_id)
        _LOGGER.info(
            "Grocy %s done: %s (product %d, EPC %s)",
            op.kind,
            op.product_name or "?",
            op.product_id,
            op.epc,
        )
        self._changed()
        if self.on_done is not None:
            done = await self.store.get_op(op.id)
            if done is not None:
                await self.on_done(done)

    def _changed(self) -> None:
        if self.on_change is not None:
            self.on_change()

    async def _verify(self, op: Op) -> float | None:
        """Consume late duplicates of an ambiguous add (same op marker)."""
        note = op_note(op)
        try:
            entries = await self.backend.entries(op.product_id)
            mine = [e for e in entries if e.note == note]
            keep = op.stock_entry_id
            if keep is None and mine:
                keep = mine[0].stock_id
            for extra in (e for e in mine if e.stock_id != keep):
                await self.backend.consume(
                    op.product_id,
                    stock_entry_id=extra.stock_id,
                    location_id=op.location_id,
                )
                self.duplicates_removed += 1
                _LOGGER.warning(
                    "Grocy applied a timed-out add for op %d late; consumed the "
                    "duplicate stock entry %s",
                    op.id,
                    extra.stock_id,
                )
        except GrocyError as err:
            delay = self.backoff.next_delay(self.rng)
            _LOGGER.warning("Duplicate check for op %d failed: %s", op.id, err)
            await self.store.schedule_verify(op.id, self.verify_clock() + delay)
            return delay
        await self.store.clear_verify(op.id)
        self._changed()
        return None

    async def _do_add(self, op: Op, ambiguous: bool) -> bool:
        note = op_note(op)
        if ambiguous:
            # An earlier attempt may still land in Grocy after this one.
            await self.store.schedule_verify(
                op.id, self.verify_clock() + self.verify_after
            )
            for entry in await self.backend.entries(op.product_id):
                if entry.note == note:
                    _LOGGER.info(
                        "Op %d already applied in Grocy (stock entry %s); not re-adding",
                        op.id,
                        entry.stock_id,
                    )
                    await self._done(op, entry.stock_id)
                    return False
            ambiguous = False
        await self.store.mark_sending(op.id)
        stock_id = await self.backend.add(
            op.product_id,
            location_id=op.location_id,
            best_before=op.best_before,
            note=note,
        )
        await self._done(op, stock_id)
        return False

    async def _do_consume(self, op: Op, ambiguous: bool) -> bool:
        add_op = await self.store.get_op(op.add_op_id) if op.add_op_id else None
        if add_op is not None and add_op.status == "failed":
            await self._fail(op, f"its add (op {add_op.id}) failed; nothing to consume")
            return False
        stock_id = add_op.stock_entry_id if add_op is not None else None
        if ambiguous and stock_id:
            entries = await self.backend.entries(op.product_id)
            if all(e.stock_id != stock_id for e in entries):
                _LOGGER.info("Op %d already applied in Grocy; not re-consuming", op.id)
                await self._done(op)
                return False
            ambiguous = False
        elif ambiguous:
            _LOGGER.warning(
                "Op %d may have reached Grocy but has no stock entry id to check; "
                "sending again",
                op.id,
            )
        await self.store.mark_sending(op.id)
        await self.backend.consume(
            op.product_id, stock_entry_id=stock_id, location_id=op.location_id
        )
        await self._done(op)
        return False
