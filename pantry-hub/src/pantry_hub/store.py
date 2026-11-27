"""SQLite persistence (``/data/pantry_hub.db``) via aiosqlite.

The hub owns only: EPC ↔ Grocy product links, tag presence, the Grocy
operation journal, unlinked tags and barcodes that need a Grocy product.
Grocy stays the source of truth for products and stock.

Every public method is one transaction. Arrival/departure bookkeeping and the
matching journal entry are written together, so a crash leaves either both or
neither (no double add, no lost add).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
import logging
from pathlib import Path
import sqlite3
from typing import Literal

import aiosqlite

_LOGGER = logging.getLogger(__name__)

SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS links (
    epc TEXT PRIMARY KEY,
    product_id INTEGER NOT NULL,
    product_name TEXT NOT NULL,
    barcode TEXT,
    best_before TEXT,
    default_best_before_days INTEGER,
    station TEXT,
    linked_at REAL NOT NULL,
    skip_add_cycle INTEGER
);
CREATE TABLE IF NOT EXISTS presence (
    epc TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    cycle INTEGER NOT NULL DEFAULT 0,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS ops (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key TEXT NOT NULL UNIQUE,
    epc TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('add', 'consume')),
    product_id INTEGER NOT NULL,
    product_name TEXT NOT NULL DEFAULT '',
    location_id INTEGER NOT NULL,
    best_before TEXT,
    add_op_id INTEGER REFERENCES ops(id),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'done', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    maybe_sent INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    stock_entry_id TEXT,
    created_at REAL NOT NULL,
    done_at REAL,
    verify_at REAL,
    resolution TEXT
);
CREATE INDEX IF NOT EXISTS ops_status ON ops (status, id);
CREATE TABLE IF NOT EXISTS unlinked (
    epc TEXT PRIMARY KEY,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    arrivals INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS needs_product (
    barcode TEXT PRIMARY KEY,
    station TEXT,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    scans INTEGER NOT NULL DEFAULT 1
);
"""

#: Columns added after schema v1 (v0.1.0): table → (column, type).
_MIGRATIONS: dict[str, list[tuple[str, str]]] = {
    "links": [("skip_add_cycle", "INTEGER")],
    "ops": [("resolution", "TEXT")],
}

OpKind = Literal["add", "consume"]
#: Why a finished/failed op ended the way it did (None = plain success/failure):
#: in_stock (add skipped: Grocy already counted the unit), review (ambiguous
#: consume, needs a person), dismissed / consumed_manually (review resolved),
#: cancelled (item left before its queued add was replayed: add + consume
#: cancelled, net zero).
Resolution = Literal[
    "in_stock", "review", "dismissed", "consumed_manually", "cancelled"
]
OpStatus = Literal["pending", "done", "failed"]


@dataclass(frozen=True, slots=True)
class Link:
    """One EPC linked to a Grocy product."""

    epc: str
    product_id: int
    product_name: str
    linked_at: float
    barcode: str | None = None
    best_before: str | None = None
    default_best_before_days: int | None = None
    station: str | None = None
    #: Presence cycle whose add is skipped because Grocy already counts the
    #: unit ("already in stock"); later cycles behave normally.
    skip_add_cycle: int | None = None


@dataclass(frozen=True, slots=True)
class Op:
    """One journaled Grocy stock change."""

    id: int
    dedupe_key: str
    epc: str
    kind: OpKind
    product_id: int
    product_name: str
    location_id: int
    best_before: str | None
    add_op_id: int | None
    status: OpStatus
    attempts: int
    maybe_sent: bool
    last_error: str | None
    stock_entry_id: str | None
    created_at: float
    done_at: float | None
    resolution: str | None = None

    @property
    def cycle(self) -> int:
        """Presence cycle this op belongs to (from its dedupe key)."""
        return int(self.dedupe_key.split(":")[1])

    @property
    def needs_review(self) -> bool:
        """Failed ambiguous consume waiting for "consume now" / "dismiss"."""
        return self.status == "failed" and self.resolution == "review"


@dataclass(frozen=True, slots=True)
class PresenceRow:
    """Persisted presence for one EPC."""

    epc: str
    state: str
    cycle: int
    first_seen: float
    last_seen: float


@dataclass(frozen=True, slots=True)
class Unlinked:
    """A tag seen in the pantry that has no link."""

    epc: str
    first_seen: float
    last_seen: float
    arrivals: int


@dataclass(frozen=True, slots=True)
class NeedsProduct:
    """A scanned barcode that Grocy does not know."""

    barcode: str
    station: str | None
    first_seen: float
    last_seen: float
    scans: int


@dataclass(frozen=True, slots=True)
class ArrivalResult:
    """What :meth:`Store.apply_arrival` did."""

    cycle: int
    link: Link | None
    add_op_id: int | None
    skipped_in_stock: bool = False


@dataclass(frozen=True, slots=True)
class DepartureResult:
    """What :meth:`Store.apply_departure` did."""

    cycle: int
    link: Link | None
    add_op: Op | None
    consume_op_id: int | None


def add_key(epc: str, cycle: int) -> str:
    """Journal dedupe key for the add of one arrival."""
    return f"{epc}:{cycle}:add"


def consume_key(epc: str, cycle: int) -> str:
    """Journal dedupe key for the consume of one departure."""
    return f"{epc}:{cycle}:consume"


def iso(ts: float) -> str:
    """Format a UNIX timestamp for display."""
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds")


_OP_COLS = (
    "id, dedupe_key, epc, kind, product_id, product_name, location_id, best_before, "
    "add_op_id, status, attempts, maybe_sent, last_error, stock_entry_id, "
    "created_at, done_at, resolution"
)
_LINK_COLS = (
    "epc, product_id, product_name, linked_at, barcode, best_before, "
    "default_best_before_days, station, skip_add_cycle"
)


def _op(row: sqlite3.Row) -> Op:
    return Op(
        id=row["id"],
        dedupe_key=row["dedupe_key"],
        epc=row["epc"],
        kind=row["kind"],
        product_id=row["product_id"],
        product_name=row["product_name"],
        location_id=row["location_id"],
        best_before=row["best_before"],
        add_op_id=row["add_op_id"],
        status=row["status"],
        attempts=row["attempts"],
        maybe_sent=bool(row["maybe_sent"]),
        last_error=row["last_error"],
        stock_entry_id=row["stock_entry_id"],
        created_at=row["created_at"],
        done_at=row["done_at"],
        resolution=row["resolution"],
    )


def _link(row: sqlite3.Row) -> Link:
    return Link(
        epc=row["epc"],
        product_id=row["product_id"],
        product_name=row["product_name"],
        linked_at=row["linked_at"],
        barcode=row["barcode"],
        best_before=row["best_before"],
        default_best_before_days=row["default_best_before_days"],
        station=row["station"],
        skip_add_cycle=row["skip_add_cycle"],
    )


class Store:
    """Async wrapper around one SQLite connection."""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        """Use :meth:`open`."""
        self._conn = conn
        self._lock = asyncio.Lock()

    @classmethod
    async def open(cls, path: Path | str) -> Store:
        """Open (creating if needed) the database at ``path``."""
        conn = await aiosqlite.connect(str(path), isolation_level=None)
        conn.row_factory = sqlite3.Row
        store = cls(conn)
        await store._exec("PRAGMA journal_mode=WAL")
        await store._exec("PRAGMA synchronous=FULL")
        await store._exec("PRAGMA foreign_keys=ON")
        cur = await conn.executescript(_SCHEMA)
        await cur.close()
        await store._migrate()
        return store

    async def _migrate(self) -> None:
        """Bring a v0.1.0 (schema 1) database up to date; add-only, idempotent."""
        for table, columns in _MIGRATIONS.items():
            have = {r["name"] for r in await self._all(f"PRAGMA table_info({table})")}
            for name, kind in columns:
                if name not in have:
                    await self._exec(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")
        await self._exec(
            "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(SCHEMA_VERSION),),
        )

    async def close(self) -> None:
        """Checkpoint and close (the connection is closed even if the checkpoint fails)."""
        async with self._lock:
            try:
                await self._exec("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error as err:
                # Committed data is already durable in the WAL (synchronous=FULL).
                _LOGGER.warning("SQLite checkpoint at shutdown failed: %s", err)
            finally:
                await self._conn.close()

    # Helpers -------------------------------------------------------------------

    async def _one(self, sql: str, args: Iterable[object] = ()) -> sqlite3.Row | None:
        async with self._conn.execute(sql, tuple(args)) as cur:
            row: sqlite3.Row | None = await cur.fetchone()
            return row

    async def _all(self, sql: str, args: Iterable[object] = ()) -> list[sqlite3.Row]:
        async with self._conn.execute(sql, tuple(args)) as cur:
            return list(await cur.fetchall())

    async def _exec(self, sql: str, args: Iterable[object] = ()) -> None:
        """Run a statement and close its cursor at once.

        A cursor left open keeps its statement active, which makes the
        shutdown checkpoint fail with "database table is locked".
        """
        cur = await self._conn.execute(sql, tuple(args))
        await cur.close()

    async def _exec_many(self, sql: str, rows: list[tuple[object, ...]]) -> None:
        cur = await self._conn.executemany(sql, rows)
        await cur.close()

    async def _begin(self) -> None:
        await self._exec("BEGIN IMMEDIATE")

    async def _commit(self) -> None:
        await self._exec("COMMIT")

    async def _rollback(self) -> None:
        await self._exec("ROLLBACK")

    async def _get_link(self, epc: str) -> Link | None:
        row = await self._one(f"SELECT {_LINK_COLS} FROM links WHERE epc = ?", (epc,))
        return None if row is None else _link(row)

    async def _get_op_by_key(self, key: str) -> Op | None:
        row = await self._one(
            f"SELECT {_OP_COLS} FROM ops WHERE dedupe_key = ?", (key,)
        )
        return None if row is None else _op(row)

    async def _insert_op(
        self,
        *,
        key: str,
        epc: str,
        kind: OpKind,
        product_id: int,
        product_name: str,
        location_id: int,
        best_before: str | None = None,
        add_op_id: int | None = None,
        now: float,
    ) -> int | None:
        cur = await self._conn.execute(
            "INSERT OR IGNORE INTO ops (dedupe_key, epc, kind, product_id, "
            "product_name, location_id, best_before, add_op_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                key,
                epc,
                kind,
                product_id,
                product_name,
                location_id,
                best_before,
                add_op_id,
                now,
            ),
        )
        inserted = cur.rowcount == 1
        op_id = cur.lastrowid
        await cur.close()
        return op_id if inserted else None

    # Presence + journal (atomic) ----------------------------------------------

    async def apply_arrival(
        self, epc: str, now: float, location_id: int, best_before: str | None = None
    ) -> ArrivalResult:
        """Record an arrival; journal an add if the EPC is linked."""
        async with self._lock:
            await self._begin()
            try:
                await self._exec(
                    "INSERT INTO presence (epc, state, cycle, first_seen, last_seen) "
                    "VALUES (?, 'present', 1, ?, ?) ON CONFLICT(epc) DO UPDATE SET "
                    "state = 'present', cycle = cycle + 1, last_seen = excluded.last_seen",
                    (epc, now, now),
                )
                row = await self._one(
                    "SELECT cycle FROM presence WHERE epc = ?", (epc,)
                )
                cycle = 1 if row is None else int(row["cycle"])
                link = await self._get_link(epc)
                op_id: int | None = None
                skipped = False
                if link is None:
                    await self._exec(
                        "INSERT INTO unlinked (epc, first_seen, last_seen) "
                        "VALUES (?, ?, ?) ON CONFLICT(epc) DO UPDATE SET "
                        "last_seen = excluded.last_seen, arrivals = arrivals + 1",
                        (epc, now, now),
                    )
                elif link.skip_add_cycle == cycle:
                    await self._insert_skipped_add(link, cycle, location_id, now)
                    skipped = True
                else:
                    op_id = await self._insert_op(
                        key=add_key(epc, cycle),
                        epc=epc,
                        kind="add",
                        product_id=link.product_id,
                        product_name=link.product_name,
                        location_id=location_id,
                        best_before=best_before,
                        now=now,
                    )
                await self._commit()
            except BaseException:
                await self._rollback()
                raise
        return ArrivalResult(
            cycle=cycle, link=link, add_op_id=op_id, skipped_in_stock=skipped
        )

    async def _insert_skipped_add(
        self, link: Link, cycle: int, location_id: int, now: float
    ) -> int | None:
        """Journal an add that is not sent: Grocy already counts this unit.

        It is recorded as done (resolution ``in_stock``), so the departure of
        this cycle still consumes one unit, at the location, by amount.
        """
        op_id = await self._insert_op(
            key=add_key(link.epc, cycle),
            epc=link.epc,
            kind="add",
            product_id=link.product_id,
            product_name=link.product_name,
            location_id=location_id,
            now=now,
        )
        if op_id is not None:
            await self._exec(
                "UPDATE ops SET status = 'done', resolution = 'in_stock', done_at = ? "
                "WHERE id = ?",
                (now, op_id),
            )
        return op_id

    async def apply_departure(self, epc: str, now: float) -> DepartureResult:
        """Record a departure; journal a consume if this cycle's add was journaled."""
        async with self._lock:
            await self._begin()
            try:
                row = await self._one(
                    "SELECT cycle FROM presence WHERE epc = ?", (epc,)
                )
                cycle = 0 if row is None else int(row["cycle"])
                await self._exec(
                    "UPDATE presence SET state = 'departed' WHERE epc = ?", (epc,)
                )
                link = await self._get_link(epc)
                add_op = (
                    await self._get_op_by_key(add_key(epc, cycle)) if cycle else None
                )
                consume_id: int | None = None
                if add_op is not None and add_op.status != "failed":
                    consume_id = await self._insert_op(
                        key=consume_key(epc, cycle),
                        epc=epc,
                        kind="consume",
                        product_id=add_op.product_id,
                        product_name=add_op.product_name,
                        location_id=add_op.location_id,
                        add_op_id=add_op.id,
                        now=now,
                    )
                await self._commit()
            except BaseException:
                await self._rollback()
                raise
        return DepartureResult(
            cycle=cycle, link=link, add_op=add_op, consume_op_id=consume_id
        )

    async def create_link(
        self,
        link: Link,
        *,
        location_id: int,
        best_before: str | None = None,
        in_stock: bool = False,
    ) -> tuple[bool, int | None]:
        """Store ``link`` unless the EPC is already linked.

        Returns ``(created, add_op_id)``. If the EPC is already in the pantry
        (an arrival happened while unlinked), an add for that arrival is
        journaled now, once. ``in_stock``: Grocy already counts this unit, so
        the add of the current presence cycle (or the next arrival, if the tag
        is not in the pantry now) is skipped.
        """
        async with self._lock:
            await self._begin()
            try:
                if await self._get_link(link.epc) is not None:
                    await self._rollback()
                    return False, None
                link = await self._with_skip(link, in_stock)
                await self._write_link(link)
                op_id = await self._add_for_present(link, location_id, best_before)
                await self._commit()
            except BaseException:
                await self._rollback()
                raise
        return True, op_id

    async def replace_link(
        self,
        link: Link,
        *,
        location_id: int,
        best_before: str | None = None,
        in_stock: bool = False,
    ) -> int | None:
        """Relink (web UI): overwrite any existing link for the EPC."""
        async with self._lock:
            await self._begin()
            try:
                link = await self._with_skip(link, in_stock)
                await self._write_link(link)
                op_id = await self._add_for_present(link, location_id, best_before)
                await self._commit()
            except BaseException:
                await self._rollback()
                raise
        return op_id

    async def _with_skip(self, link: Link, in_stock: bool) -> Link:
        """Set ``skip_add_cycle`` for an "already in stock" link."""
        if not in_stock:
            return link
        row = await self._one(
            "SELECT state, cycle FROM presence WHERE epc = ?", (link.epc,)
        )
        cycle = 0 if row is None else int(row["cycle"])
        present = row is not None and row["state"] == "present" and cycle > 0
        return replace(link, skip_add_cycle=cycle if present else cycle + 1)

    async def _write_link(self, link: Link) -> None:
        await self._exec(
            f"INSERT OR REPLACE INTO links ({_LINK_COLS}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                link.epc,
                link.product_id,
                link.product_name,
                link.linked_at,
                link.barcode,
                link.best_before,
                link.default_best_before_days,
                link.station,
                link.skip_add_cycle,
            ),
        )
        await self._exec("DELETE FROM unlinked WHERE epc = ?", (link.epc,))

    async def _add_for_present(
        self, link: Link, location_id: int, best_before: str | None
    ) -> int | None:
        row = await self._one(
            "SELECT state, cycle FROM presence WHERE epc = ?", (link.epc,)
        )
        if row is None or row["state"] != "present" or not row["cycle"]:
            return None
        if link.skip_add_cycle == int(row["cycle"]):
            await self._insert_skipped_add(
                link, int(row["cycle"]), location_id, link.linked_at
            )
            return None
        return await self._insert_op(
            key=add_key(link.epc, int(row["cycle"])),
            epc=link.epc,
            kind="add",
            product_id=link.product_id,
            product_name=link.product_name,
            location_id=location_id,
            best_before=best_before,
            now=link.linked_at,
        )

    # Meta ----------------------------------------------------------------------

    async def get_meta(self, key: str) -> str | None:
        """Return a stored setting (e.g. the reader's unique id)."""
        async with self._lock:
            row = await self._one("SELECT value FROM meta WHERE key = ?", (key,))
        return None if row is None else str(row["value"])

    async def set_meta(self, key: str, value: str) -> None:
        """Store a setting."""
        async with self._lock:
            await self._exec(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    # Links ---------------------------------------------------------------------

    async def get_link(self, epc: str) -> Link | None:
        """Return the link for ``epc``, if any."""
        async with self._lock:
            return await self._get_link(epc)

    async def links(self) -> list[Link]:
        """Return every link, newest first."""
        async with self._lock:
            rows = await self._all(
                f"SELECT {_LINK_COLS} FROM links ORDER BY linked_at DESC, epc"
            )
        return [_link(r) for r in rows]

    async def delete_link(self, epc: str) -> bool:
        """Unlink ``epc``; return whether it was linked."""
        async with self._lock:
            cur = await self._conn.execute("DELETE FROM links WHERE epc = ?", (epc,))
            deleted = cur.rowcount == 1
            await cur.close()
        return deleted

    # Presence ------------------------------------------------------------------

    async def present_epcs(self) -> list[str]:
        """EPCs persisted as in the pantry (for the restart grace window)."""
        async with self._lock:
            rows = await self._all(
                "SELECT epc FROM presence WHERE state = 'present' ORDER BY epc"
            )
        return [r["epc"] for r in rows]

    async def presence_rows(self) -> list[PresenceRow]:
        """Return all persisted presence rows."""
        async with self._lock:
            rows = await self._all(
                "SELECT epc, state, cycle, first_seen, last_seen FROM presence"
            )
        return [
            PresenceRow(
                r["epc"], r["state"], r["cycle"], r["first_seen"], r["last_seen"]
            )
            for r in rows
        ]

    async def touch(self, last_seen: Mapping[str, float]) -> None:
        """Persist last-seen times (batched) for EPCs that have a presence row."""
        if not last_seen:
            return
        async with self._lock:
            await self._begin()
            try:
                await self._exec_many(
                    "UPDATE presence SET last_seen = MAX(last_seen, ?) WHERE epc = ?",
                    [(ts, epc) for epc, ts in last_seen.items()],
                )
                await self._exec_many(
                    "UPDATE unlinked SET last_seen = MAX(last_seen, ?) WHERE epc = ?",
                    [(ts, epc) for epc, ts in last_seen.items()],
                )
                await self._commit()
            except BaseException:
                await self._rollback()
                raise

    # Journal -------------------------------------------------------------------

    async def next_pending(self) -> Op | None:
        """Oldest pending op (the queue replays strictly in order)."""
        async with self._lock:
            row = await self._one(
                f"SELECT {_OP_COLS} FROM ops WHERE status = 'pending' ORDER BY id LIMIT 1"
            )
        return None if row is None else _op(row)

    async def get_op(self, op_id: int) -> Op | None:
        """Return one op by id."""
        async with self._lock:
            row = await self._one(f"SELECT {_OP_COLS} FROM ops WHERE id = ?", (op_id,))
        return None if row is None else _op(row)

    async def ops(self, status: OpStatus | None = None, limit: int = 200) -> list[Op]:
        """Return ops, newest first."""
        async with self._lock:
            if status is None:
                rows = await self._all(
                    f"SELECT {_OP_COLS} FROM ops ORDER BY id DESC LIMIT ?", (limit,)
                )
            else:
                rows = await self._all(
                    f"SELECT {_OP_COLS} FROM ops WHERE status = ? ORDER BY id DESC LIMIT ?",
                    (status, limit),
                )
        return [_op(r) for r in rows]

    async def mark_sending(self, op_id: int) -> None:
        """Record that a request for ``op_id`` is about to be sent."""
        async with self._lock:
            await self._exec(
                "UPDATE ops SET attempts = attempts + 1, maybe_sent = 1 WHERE id = ?",
                (op_id,),
            )

    async def mark_retry(self, op_id: int, error: str, *, maybe_sent: bool) -> None:
        """Record a transient failure."""
        async with self._lock:
            await self._exec(
                "UPDATE ops SET last_error = ?, maybe_sent = ? WHERE id = ?",
                (error[:500], int(maybe_sent), op_id),
            )

    async def mark_done(
        self, op_id: int, now: float, stock_entry_id: str | None = None
    ) -> None:
        """Record success."""
        async with self._lock:
            await self._exec(
                "UPDATE ops SET status = 'done', done_at = ?, maybe_sent = 0, "
                "last_error = NULL, stock_entry_id = COALESCE(?, stock_entry_id) "
                "WHERE id = ?",
                (now, stock_entry_id, op_id),
            )

    async def mark_failed(self, op_id: int, error: str, now: float) -> None:
        """Record a permanent failure (kept for the UI; never silently dropped)."""
        async with self._lock:
            await self._exec(
                "UPDATE ops SET status = 'failed', last_error = ?, done_at = ? "
                "WHERE id = ?",
                (error[:500], now, op_id),
            )

    async def retry_failed(self, op_id: int) -> bool:
        """Put a failed op back in the queue (web UI)."""
        async with self._lock:
            cur = await self._conn.execute(
                "UPDATE ops SET status = 'pending', done_at = NULL "
                "WHERE id = ? AND status = 'failed' "
                "AND (resolution IS NULL OR resolution != 'review')",
                (op_id,),
            )
            changed = cur.rowcount == 1
            await cur.close()
        return changed

    async def counts(self) -> dict[str, int]:
        """Pending/failed op counts (``failed`` includes ``review``)."""
        async with self._lock:
            rows = await self._all(
                "SELECT status, COUNT(*) AS n FROM ops GROUP BY status"
            )
            extra = await self._one(
                "SELECT SUM(verify_at IS NOT NULL) AS verifying, "
                "SUM(status = 'failed' AND resolution = 'review') AS review FROM ops"
            )
        out = {"pending": 0, "done": 0, "failed": 0}
        for r in rows:
            out[r["status"]] = int(r["n"])
        out["verifying"] = int(extra["verifying"] or 0) if extra is not None else 0
        out["review"] = int(extra["review"] or 0) if extra is not None else 0
        return out

    async def mark_review(self, op_id: int, error: str, now: float) -> None:
        """Fail an ambiguous consume for a person to decide (never resent)."""
        async with self._lock:
            await self._exec(
                "UPDATE ops SET status = 'failed', resolution = 'review', "
                "last_error = ?, done_at = ? WHERE id = ?",
                (error[:500], now, op_id),
            )

    async def resolve_review(
        self,
        op_id: int,
        resolution: Literal["dismissed", "consumed_manually"],
        now: float,
    ) -> bool:
        """Close a review op (web UI "dismiss" / "consume now")."""
        async with self._lock:
            cur = await self._conn.execute(
                "UPDATE ops SET status = 'done', resolution = ?, done_at = ? "
                "WHERE id = ? AND status = 'failed' AND resolution = 'review'",
                (resolution, now, op_id),
            )
            changed = cur.rowcount == 1
            await cur.close()
        return changed

    async def presence(self, epc: str) -> PresenceRow | None:
        """Return the persisted presence of one EPC."""
        async with self._lock:
            r = await self._one(
                "SELECT epc, state, cycle, first_seen, last_seen FROM presence "
                "WHERE epc = ?",
                (epc,),
            )
        if r is None:
            return None
        return PresenceRow(
            r["epc"], r["state"], r["cycle"], r["first_seen"], r["last_seen"]
        )

    async def cancel_cycle(self, add_op: Op, now: float) -> int | None:
        """Cancel a queued add and its consume (item already left): net zero.

        Returns the id of the consume op that was cancelled, if any.
        """
        async with self._lock:
            await self._begin()
            try:
                await self._exec(
                    "UPDATE ops SET status = 'done', resolution = 'cancelled', "
                    "done_at = ?, maybe_sent = 0 WHERE id = ?",
                    (now, add_op.id),
                )
                consume = await self._get_op_by_key(
                    consume_key(add_op.epc, add_op.cycle)
                )
                cancelled: int | None = None
                if consume is not None and consume.status != "done":
                    await self._exec(
                        "UPDATE ops SET status = 'done', resolution = 'cancelled', "
                        "done_at = ? WHERE id = ?",
                        (now, consume.id),
                    )
                    cancelled = consume.id
                await self._commit()
            except BaseException:
                await self._rollback()
                raise
        return cancelled

    async def schedule_verify(self, op_id: int, at: float) -> None:
        """Re-check Grocy for late duplicates of an ambiguous add at ``at``."""
        async with self._lock:
            await self._exec("UPDATE ops SET verify_at = ? WHERE id = ?", (at, op_id))

    async def next_verify(self) -> tuple[Op, float] | None:
        """Return the op whose duplicate check is due first, and its due time."""
        async with self._lock:
            row = await self._one(
                f"SELECT {_OP_COLS}, verify_at FROM ops "
                "WHERE verify_at IS NOT NULL ORDER BY verify_at LIMIT 1"
            )
        return None if row is None else (_op(row), float(row["verify_at"]))

    async def clear_verify(self, op_id: int) -> None:
        """Mark the duplicate check of ``op_id`` as finished."""
        async with self._lock:
            await self._exec("UPDATE ops SET verify_at = NULL WHERE id = ?", (op_id,))

    async def stocked_epcs(self) -> dict[int, list[str]]:
        """Per product: EPCs whose current arrival was added and not consumed."""
        async with self._lock:
            rows = await self._all(
                "SELECT p.epc, a.product_id FROM presence p "
                "JOIN ops a ON a.dedupe_key = p.epc || ':' || p.cycle || ':add' "
                "WHERE p.state = 'present' AND a.status = 'done'"
            )
        out: dict[int, list[str]] = {}
        for r in rows:
            out.setdefault(int(r["product_id"]), []).append(r["epc"])
        return out

    # Unlinked + needs product --------------------------------------------------

    async def unlinked(self) -> list[Unlinked]:
        """Return unlinked tags, most recently seen first."""
        async with self._lock:
            rows = await self._all(
                "SELECT epc, first_seen, last_seen, arrivals FROM unlinked "
                "ORDER BY last_seen DESC"
            )
        return [
            Unlinked(r["epc"], r["first_seen"], r["last_seen"], r["arrivals"])
            for r in rows
        ]

    async def forget_unlinked(self, epc: str) -> bool:
        """Remove ``epc`` from the unlinked list."""
        async with self._lock:
            cur = await self._conn.execute("DELETE FROM unlinked WHERE epc = ?", (epc,))
            deleted = cur.rowcount == 1
            await cur.close()
        return deleted

    async def add_needs_product(self, barcode: str, station: str, now: float) -> None:
        """Remember a barcode Grocy does not know."""
        async with self._lock:
            await self._exec(
                "INSERT INTO needs_product (barcode, station, first_seen, last_seen) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(barcode) DO UPDATE SET "
                "last_seen = excluded.last_seen, scans = scans + 1, "
                "station = excluded.station",
                (barcode, station, now, now),
            )

    async def needs_product(self) -> list[NeedsProduct]:
        """Return barcodes waiting for a Grocy product."""
        async with self._lock:
            rows = await self._all(
                "SELECT barcode, station, first_seen, last_seen, scans "
                "FROM needs_product ORDER BY last_seen DESC"
            )
        return [
            NeedsProduct(
                r["barcode"], r["station"], r["first_seen"], r["last_seen"], r["scans"]
            )
            for r in rows
        ]

    async def remove_needs_product(self, barcode: str) -> bool:
        """Drop a barcode from the needs-product list."""
        async with self._lock:
            cur = await self._conn.execute(
                "DELETE FROM needs_product WHERE barcode = ?", (barcode,)
            )
            deleted = cur.rowcount == 1
            await cur.close()
        return deleted
