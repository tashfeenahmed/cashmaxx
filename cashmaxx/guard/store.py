"""sqlite store for the guard: ledger, payments, approvals, events and key-value state.

One connection, one lock, every call run in a worker thread so the event loop never blocks on disk.
Amounts are stored as TEXT decimals and times as fixed-width ISO-8601 UTC strings (``...Z``) so
string comparison orders them correctly.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, TypeVar

from cashmaxx.money import ZERO, fmt_usd

T = TypeVar("T")

Direction = Literal["income", "cost"]
PaymentStatus = Literal["pending", "paid", "denied", "failed"]
OutboundChannel = Literal["email", "social"]
OutboundStatus = Literal["pending", "sent", "failed"]
ApprovalStatus = Literal["pending", "approved", "denied", "expired"]
Actor = Literal["agent", "owner", "guard"]

INCOME_CATEGORIES = frozenset({"sale_stripe", "sale_x402", "transfer_in", "bounty", "marketplace"})
COST_CATEGORIES = frozenset({"payment_out", "compute", "reimbursement", "fees", "other"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('income', 'cost')),
    category TEXT NOT NULL,
    amount_usd TEXT NOT NULL,
    source TEXT NOT NULL,
    ref TEXT,
    note TEXT,
    verified INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS ledger_source_ref ON ledger(source, ref) WHERE ref IS NOT NULL;
CREATE INDEX IF NOT EXISTS ledger_ts ON ledger(ts);
CREATE TABLE IF NOT EXISTS payments (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    to_addr TEXT NOT NULL,
    amount_usd TEXT NOT NULL,
    purpose TEXT NOT NULL,
    category TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'paid', 'denied', 'failed')),
    reason TEXT,
    approval_id TEXT,
    tx_hash TEXT
);
CREATE INDEX IF NOT EXISTS payments_ts ON payments(ts);
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    payment_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'denied', 'expired')),
    decided_ts TEXT,
    decided_via TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    type TEXT NOT NULL,
    data_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outbound (
    id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    channel TEXT NOT NULL CHECK (channel IN ('email', 'social')),
    provider TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    recipients INTEGER NOT NULL,
    target TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'sent', 'failed')),
    ref TEXT
);
CREATE INDEX IF NOT EXISTS outbound_channel_ts ON outbound(channel, ts);
"""


def iso(dt: datetime) -> str:
    """Fixed-width UTC timestamp, sortable as a string."""
    if dt.tzinfo is None:
        raise ValueError("naive datetime")
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


@dataclass(frozen=True)
class LedgerEntry:
    id: int
    ts: str
    direction: Direction
    category: str
    amount_usd: Decimal
    source: str
    ref: str | None
    note: str | None
    verified: bool

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id, "ts": self.ts, "direction": self.direction, "category": self.category,
            "amount_usd": fmt_usd(self.amount_usd), "source": self.source, "ref": self.ref,
            "note": self.note, "verified": self.verified,
        }


@dataclass(frozen=True)
class Payment:
    id: str
    idempotency_key: str
    ts: str
    kind: str
    to_addr: str
    amount_usd: Decimal
    purpose: str
    category: str
    status: PaymentStatus
    reason: str | None
    approval_id: str | None
    tx_hash: str | None

    def to_json(self) -> dict[str, Any]:
        return {
            "payment_id": self.id, "status": self.status, "kind": self.kind, "to": self.to_addr,
            "amount_usd": fmt_usd(self.amount_usd), "purpose": self.purpose,
            "category": self.category, "reason": self.reason, "approval_id": self.approval_id,
            "tx_hash": self.tx_hash, "ts": self.ts, "idempotency_key": self.idempotency_key,
        }


@dataclass(frozen=True)
class Approval:
    id: str
    ts: str
    payment_id: str
    reason: str
    status: ApprovalStatus
    decided_ts: str | None
    decided_via: str | None

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id, "ts": self.ts, "payment_id": self.payment_id, "reason": self.reason,
            "status": self.status, "decided_ts": self.decided_ts, "decided_via": self.decided_via,
        }


@dataclass(frozen=True)
class Event:
    id: int
    ts: str
    actor: str
    type: str
    data: dict[str, Any]

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "ts": self.ts, "actor": self.actor, "type": self.type,
                "data": self.data}


@dataclass(frozen=True)
class Outbound:
    """One outbound email or social post: counts toward the daily caps and holds idempotency."""

    id: str
    ts: str
    channel: OutboundChannel
    provider: str
    idempotency_key: str
    recipients: int
    target: str
    status: OutboundStatus
    ref: str | None


def _outbound(row: sqlite3.Row) -> Outbound:
    return Outbound(
        id=row["id"], ts=row["ts"], channel=row["channel"], provider=row["provider"],
        idempotency_key=row["idempotency_key"], recipients=int(row["recipients"]),
        target=row["target"], status=row["status"], ref=row["ref"],
    )


def _ledger(row: sqlite3.Row) -> LedgerEntry:
    return LedgerEntry(
        id=row["id"], ts=row["ts"], direction=row["direction"], category=row["category"],
        amount_usd=Decimal(row["amount_usd"]), source=row["source"], ref=row["ref"],
        note=row["note"], verified=bool(row["verified"]),
    )


def _payment(row: sqlite3.Row) -> Payment:
    return Payment(
        id=row["id"], idempotency_key=row["idempotency_key"], ts=row["ts"], kind=row["kind"],
        to_addr=row["to_addr"], amount_usd=Decimal(row["amount_usd"]), purpose=row["purpose"],
        category=row["category"], status=row["status"], reason=row["reason"],
        approval_id=row["approval_id"], tx_hash=row["tx_hash"],
    )


def _approval(row: sqlite3.Row) -> Approval:
    return Approval(
        id=row["id"], ts=row["ts"], payment_id=row["payment_id"], reason=row["reason"],
        status=row["status"], decided_ts=row["decided_ts"], decided_via=row["decided_via"],
    )


def _sum(rows: list[sqlite3.Row], key: str = "amount_usd") -> Decimal:
    return sum((Decimal(r[key]) for r in rows), ZERO)


class DuplicateIdempotencyKeyError(Exception):
    """Raised when a payment with the same idempotency key already exists."""


class Store:
    """Typed accessors over the guard's sqlite file. Safe to share across tasks."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(fd)
            os.chmod(self.path, 0o600)
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    async def _run(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        def call() -> T:
            with self._lock:
                return fn(self._conn)

        return await asyncio.to_thread(call)

    # --- ledger ---------------------------------------------------------------------------------
    async def add_ledger(
        self,
        *,
        ts: datetime,
        direction: Direction,
        category: str,
        amount: Decimal,
        source: str,
        ref: str | None = None,
        note: str | None = None,
        verified: bool = False,
    ) -> int | None:
        """Insert a ledger row. Returns ``None`` if ``(source, ref)`` was already recorded."""

        def fn(c: sqlite3.Connection) -> int | None:
            cur = c.execute(
                "INSERT OR IGNORE INTO ledger (ts, direction, category, amount_usd, source, ref,"
                " note, verified) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (iso(ts), direction, category, str(amount), source, ref, note, int(verified)),
            )
            return cur.lastrowid if cur.rowcount else None

        return await self._run(fn)

    async def ledger_entries(self, since: datetime | None = None) -> list[LedgerEntry]:
        def fn(c: sqlite3.Connection) -> list[LedgerEntry]:
            if since is None:
                rows = c.execute("SELECT * FROM ledger ORDER BY ts DESC, id DESC").fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM ledger WHERE ts >= ? ORDER BY ts DESC, id DESC", (iso(since),)
                ).fetchall()
            return [_ledger(r) for r in rows]

        return await self._run(fn)

    async def sum_ledger(
        self,
        *,
        direction: Direction | None = None,
        categories: frozenset[str] | set[str] | None = None,
        source: str | None = None,
        since: datetime | None = None,
    ) -> Decimal:
        clauses: list[str] = []
        args: list[Any] = []
        if direction:
            clauses.append("direction = ?")
            args.append(direction)
        if categories is not None:
            cats = sorted(categories)
            clauses.append(f"category IN ({','.join('?' * len(cats))})")
            args.extend(cats)
        if source:
            clauses.append("source = ?")
            args.append(source)
        if since is not None:
            clauses.append("ts >= ?")
            args.append(iso(since))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""

        def fn(c: sqlite3.Connection) -> Decimal:
            return _sum(c.execute(f"SELECT amount_usd FROM ledger{where}", args).fetchall())

        return await self._run(fn)

    # --- payments -------------------------------------------------------------------------------
    async def insert_payment(
        self,
        *,
        ts: datetime,
        idempotency_key: str,
        kind: str,
        to_addr: str,
        amount: Decimal,
        purpose: str,
        category: str,
        status: PaymentStatus,
        reason: str | None = None,
    ) -> Payment:
        pid = new_id("pay")

        def fn(c: sqlite3.Connection) -> Payment:
            try:
                c.execute(
                    "INSERT INTO payments (id, idempotency_key, ts, kind, to_addr, amount_usd,"
                    " purpose, category, status, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (pid, idempotency_key, iso(ts), kind, to_addr, str(amount), purpose,
                     category, status, reason),
                )
            except sqlite3.IntegrityError as exc:
                raise DuplicateIdempotencyKeyError(idempotency_key) from exc
            return _payment(c.execute("SELECT * FROM payments WHERE id = ?", (pid,)).fetchone())

        return await self._run(fn)

    async def get_payment(self, payment_id: str) -> Payment | None:
        def fn(c: sqlite3.Connection) -> Payment | None:
            row = c.execute("SELECT * FROM payments WHERE id = ?", (payment_id,)).fetchone()
            return _payment(row) if row else None

        return await self._run(fn)

    async def get_payment_by_key(self, key: str) -> Payment | None:
        def fn(c: sqlite3.Connection) -> Payment | None:
            row = c.execute("SELECT * FROM payments WHERE idempotency_key = ?", (key,)).fetchone()
            return _payment(row) if row else None

        return await self._run(fn)

    async def update_payment(self, payment_id: str, **fields: Any) -> Payment:
        allowed = {"status", "reason", "approval_id", "tx_hash", "amount_usd"}
        if not fields or set(fields) - allowed:
            raise ValueError(f"bad payment fields: {sorted(fields)}")
        cols = ", ".join(f"{k} = ?" for k in fields)
        values = [str(v) if isinstance(v, Decimal) else v for v in fields.values()]

        def fn(c: sqlite3.Connection) -> Payment:
            c.execute(f"UPDATE payments SET {cols} WHERE id = ?", (*values, payment_id))
            return _payment(c.execute("SELECT * FROM payments WHERE id = ?",
                                      (payment_id,)).fetchone())

        return await self._run(fn)

    async def count_payments_since(
        self, since: datetime, *, exclude_id: str | None = None
    ) -> int:
        """Payments requested since ``since`` that are paid or still pending."""

        def fn(c: sqlite3.Connection) -> int:
            row = c.execute(
                "SELECT COUNT(*) FROM payments WHERE ts >= ? AND status IN ('paid', 'pending')"
                " AND id != ?",
                (iso(since), exclude_id or ""),
            ).fetchone()
            return int(row[0])

        return await self._run(fn)

    async def known_recipients(self) -> set[str]:
        def fn(c: sqlite3.Connection) -> set[str]:
            rows = c.execute("SELECT DISTINCT to_addr FROM payments WHERE status = 'paid'")
            return {str(r[0]).lower() for r in rows.fetchall()}

        return await self._run(fn)

    async def sum_pending_payments(self, category: str) -> Decimal:
        def fn(c: sqlite3.Connection) -> Decimal:
            return _sum(c.execute(
                "SELECT amount_usd FROM payments WHERE status = 'pending' AND category = ?",
                (category,),
            ).fetchall())

        return await self._run(fn)

    # --- approvals ------------------------------------------------------------------------------
    async def create_approval(self, *, ts: datetime, payment_id: str, reason: str) -> Approval:
        aid = new_id("apr")

        def fn(c: sqlite3.Connection) -> Approval:
            c.execute("BEGIN")
            try:
                c.execute(
                    "INSERT INTO approvals (id, ts, payment_id, reason, status)"
                    " VALUES (?, ?, ?, ?, 'pending')",
                    (aid, iso(ts), payment_id, reason),
                )
                c.execute("UPDATE payments SET approval_id = ? WHERE id = ?", (aid, payment_id))
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise
            return _approval(c.execute("SELECT * FROM approvals WHERE id = ?", (aid,)).fetchone())

        return await self._run(fn)

    async def get_approval(self, approval_id: str) -> Approval | None:
        def fn(c: sqlite3.Connection) -> Approval | None:
            row = c.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
            return _approval(row) if row else None

        return await self._run(fn)

    async def list_approvals(self, status: str | None = None, limit: int = 200) -> list[Approval]:
        def fn(c: sqlite3.Connection) -> list[Approval]:
            if status:
                rows = c.execute(
                    "SELECT * FROM approvals WHERE status = ? ORDER BY ts DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM approvals ORDER BY ts DESC LIMIT ?", (limit,)
                ).fetchall()
            return [_approval(r) for r in rows]

        return await self._run(fn)

    async def decide_approval(
        self, approval_id: str, *, status: ApprovalStatus, ts: datetime, via: str | None
    ) -> Approval | None:
        """Move a *pending* approval to ``status``. Returns ``None`` if it was not pending."""

        def fn(c: sqlite3.Connection) -> Approval | None:
            cur = c.execute(
                "UPDATE approvals SET status = ?, decided_ts = ?, decided_via = ?"
                " WHERE id = ? AND status = 'pending'",
                (status, iso(ts), via, approval_id),
            )
            if not cur.rowcount:
                return None
            return _approval(c.execute("SELECT * FROM approvals WHERE id = ?",
                                       (approval_id,)).fetchone())

        return await self._run(fn)

    async def pending_approvals_before(self, cutoff: datetime) -> list[Approval]:
        def fn(c: sqlite3.Connection) -> list[Approval]:
            rows = c.execute(
                "SELECT * FROM approvals WHERE status = 'pending' AND ts < ?", (iso(cutoff),)
            ).fetchall()
            return [_approval(r) for r in rows]

        return await self._run(fn)

    # --- events ---------------------------------------------------------------------------------
    async def add_event(
        self, *, ts: datetime, actor: Actor, type: str, data: dict[str, Any] | None = None
    ) -> int:
        payload = json.dumps(data or {}, default=str, sort_keys=True)

        def fn(c: sqlite3.Connection) -> int:
            cur = c.execute(
                "INSERT INTO events (ts, actor, type, data_json) VALUES (?, ?, ?, ?)",
                (iso(ts), actor, type, payload),
            )
            return int(cur.lastrowid or 0)

        return await self._run(fn)

    async def events(self, since: datetime | None = None, limit: int = 200) -> list[Event]:
        def fn(c: sqlite3.Connection) -> list[Event]:
            if since is None:
                rows = c.execute(
                    "SELECT * FROM (SELECT * FROM events ORDER BY id DESC LIMIT ?) ORDER BY id",
                    (limit,),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM events WHERE ts > ? ORDER BY id LIMIT ?", (iso(since), limit)
                ).fetchall()
            return [
                Event(id=r["id"], ts=r["ts"], actor=r["actor"], type=r["type"],
                      data=json.loads(r["data_json"]))
                for r in rows
            ]

        return await self._run(fn)

    # --- outbound (email + social) ----------------------------------------------------------------
    async def reserve_outbound(
        self,
        *,
        ts: datetime,
        channel: OutboundChannel,
        provider: str,
        idempotency_key: str,
        recipients: int,
        target: str,
    ) -> Outbound:
        """Insert a ``pending`` row. Raises ``DuplicateIdempotencyKeyError`` if the key exists."""
        oid = new_id("out")

        def fn(c: sqlite3.Connection) -> Outbound:
            try:
                c.execute(
                    "INSERT INTO outbound (id, ts, channel, provider, idempotency_key, recipients,"
                    " target, status) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')",
                    (oid, iso(ts), channel, provider, idempotency_key, recipients, target[:500]),
                )
            except sqlite3.IntegrityError as exc:
                raise DuplicateIdempotencyKeyError(idempotency_key) from exc
            return _outbound(c.execute("SELECT * FROM outbound WHERE id = ?", (oid,)).fetchone())

        return await self._run(fn)

    async def get_outbound_by_key(self, key: str) -> Outbound | None:
        def fn(c: sqlite3.Connection) -> Outbound | None:
            row = c.execute("SELECT * FROM outbound WHERE idempotency_key = ?", (key,)).fetchone()
            return _outbound(row) if row else None

        return await self._run(fn)

    async def finish_outbound(
        self, outbound_id: str, *, status: OutboundStatus, ref: str | None
    ) -> None:
        """Mark a reserved row sent or failed. A failed row releases its idempotency key."""

        def fn(c: sqlite3.Connection) -> None:
            if status == "failed":
                c.execute(
                    "UPDATE outbound SET status = 'failed', ref = ?,"
                    " idempotency_key = idempotency_key || ':failed:' || id WHERE id = ?",
                    (ref, outbound_id),
                )
            else:
                c.execute("UPDATE outbound SET status = ?, ref = ? WHERE id = ?",
                          (status, ref, outbound_id))

        await self._run(fn)

    async def count_outbound(self, channel: OutboundChannel, since: datetime) -> int:
        """Recipients (email) or posts (social) since ``since`` that were sent or are in flight."""

        def fn(c: sqlite3.Connection) -> int:
            row = c.execute(
                "SELECT COALESCE(SUM(recipients), 0) FROM outbound WHERE channel = ? AND ts >= ?"
                " AND status IN ('pending', 'sent')",
                (channel, iso(since)),
            ).fetchone()
            return int(row[0])

        return await self._run(fn)

    # --- kv -------------------------------------------------------------------------------------
    async def kv_get(self, key: str) -> str | None:
        def fn(c: sqlite3.Connection) -> str | None:
            row = c.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
            return str(row[0]) if row else None

        return await self._run(fn)

    async def kv_set(self, key: str, value: str) -> None:
        def fn(c: sqlite3.Connection) -> None:
            c.execute(
                "INSERT INTO kv (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

        await self._run(fn)
