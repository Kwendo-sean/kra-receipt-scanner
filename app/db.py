"""SQLite storage (WAL) + a DB-backed job queue. No Redis needed."""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Iterator

from app import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS receipts (
    id INTEGER PRIMARY KEY,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'capturing',   -- capturing|queued|processing|done|failed
    stage TEXT,
    error TEXT,
    mode TEXT NOT NULL DEFAULT 'single',        -- single|multi
    ocr_mode TEXT NOT NULL DEFAULT 'auto',      -- auto|stitch|dedupe
    parsed_json TEXT,
    corrections_json TEXT NOT NULL DEFAULT '{}',
    checks_json TEXT,
    ocr_text TEXT,
    lines_json TEXT,
    stitch_json TEXT,
    prep_json TEXT,
    stitched_path TEXT,
    kra_qr TEXT,
    category TEXT,
    deleted INTEGER NOT NULL DEFAULT 0,
    -- denormalised effective fields for fast filtering
    receipt_date TEXT,
    seller_name TEXT,
    seller_pin TEXT,
    total REAL,
    vat REAL
);
CREATE INDEX IF NOT EXISTS ix_receipts_date ON receipts(receipt_date) WHERE deleted = 0;
CREATE INDEX IF NOT EXISTS ix_receipts_status ON receipts(status);

CREATE TABLE IF NOT EXISTS frames (
    id INTEGER PRIMARY KEY,
    receipt_id INTEGER NOT NULL REFERENCES receipts(id),
    idx INTEGER NOT NULL,
    path TEXT NOT NULL,
    created_at REAL NOT NULL,
    sharpness REAL,
    width INTEGER,
    height INTEGER,
    UNIQUE(receipt_id, idx)
);

CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY,
    receipt_id INTEGER,
    at REAL NOT NULL,
    actor TEXT,
    action TEXT NOT NULL,
    field TEXT,
    old TEXT,
    new TEXT
);
CREATE INDEX IF NOT EXISTS ix_audit_receipt ON audit(receipt_id);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'queued',     -- queued|running|done|failed
    priority INTEGER NOT NULL DEFAULT 5,
    attempts INTEGER NOT NULL DEFAULT 0,
    run_after REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    error TEXT,
    result_json TEXT
);
CREATE INDEX IF NOT EXISTS ix_jobs_pick ON jobs(status, priority, run_after);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,          -- sha256 of the cookie token
    created_at REAL NOT NULL,
    last_seen REAL NOT NULL,
    expires_at REAL NOT NULL,
    user_agent TEXT,
    label TEXT,
    revoked INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS pair_tokens (
    token_hash TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    used_at REAL
);

CREATE TABLE IF NOT EXISTS pair_attempts (
    ip TEXT NOT NULL,
    at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS recipients (
    id INTEGER PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    confirmed INTEGER NOT NULL DEFAULT 0,
    confirm_code_hash TEXT,
    created_at REAL NOT NULL,
    confirmed_at REAL
);

CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY,
    created_at REAL NOT NULL,
    group_id TEXT NOT NULL,
    part INTEGER NOT NULL DEFAULT 1,
    parts INTEGER NOT NULL DEFAULT 1,
    recipient TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    attachments_json TEXT NOT NULL DEFAULT '[]',
    size_bytes INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'queued',     -- queued|sending|sent|failed
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt REAL NOT NULL DEFAULT 0,
    last_error TEXT,
    sent_at REAL
);

CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY,
    created_at REAL NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    params_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    files_json TEXT NOT NULL DEFAULT '{}',
    prose_status TEXT,
    error TEXT
);

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def connect() -> sqlite3.Connection:
    s = config.settings
    s.ensure_dirs()
    con = sqlite3.connect(s.db_path, timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=30000")
    return con


@contextmanager
def conn() -> Iterator[sqlite3.Connection]:
    c = connect()
    try:
        yield c
    finally:
        c.close()


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    c = connect()
    try:
        c.execute("BEGIN IMMEDIATE")
        yield c
        c.execute("COMMIT")
    except BaseException:
        c.execute("ROLLBACK")
        raise
    finally:
        c.close()


def init_db() -> None:
    with conn() as c:
        c.executescript(SCHEMA)


def one(sql: str, *args) -> sqlite3.Row | None:
    with conn() as c:
        return c.execute(sql, args).fetchone()


def all_(sql: str, *args) -> list[sqlite3.Row]:
    with conn() as c:
        return c.execute(sql, args).fetchall()


def run(sql: str, *args) -> int:
    with conn() as c:
        cur = c.execute(sql, args)
        return cur.lastrowid


def jdump(v: Any) -> str:
    return json.dumps(v, default=str, ensure_ascii=False)


def jload(s: str | None, default: Any = None) -> Any:
    if not s:
        return default
    try:
        return json.loads(s)
    except ValueError:
        return default


# ------------------------------------------------------------------ kv


def kv_get(key: str, default: Any = None) -> Any:
    r = one("SELECT value FROM kv WHERE key = ?", key)
    return jload(r["value"], default) if r else default


def kv_set(key: str, value: Any) -> None:
    run("INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        key, jdump(value))


# ------------------------------------------------------------------ audit


def audit(receipt_id: int | None, action: str, field: str | None = None, old: Any = None, new: Any = None,
          actor: str = "user") -> None:
    run("INSERT INTO audit(receipt_id, at, actor, action, field, old, new) VALUES (?,?,?,?,?,?,?)",
        receipt_id, time.time(), actor, action, field,
        None if old is None else jdump(old), None if new is None else jdump(new))


# ------------------------------------------------------------------ jobs


def enqueue(kind: str, payload: dict | None = None, priority: int = 5, run_after: float = 0) -> int:
    return run("INSERT INTO jobs(kind, payload_json, priority, run_after, created_at) VALUES (?,?,?,?,?)",
               kind, jdump(payload or {}), priority, run_after, time.time())


def claim_job(kinds: list[str] | None = None) -> sqlite3.Row | None:
    """Atomically claim the next runnable job (one worker; safe with several)."""
    now = time.time()
    with tx() as c:
        q = "SELECT * FROM jobs WHERE status = 'queued' AND run_after <= ?"
        args: list = [now]
        if kinds:
            q += " AND kind IN (%s)" % ",".join("?" * len(kinds))
            args += kinds
        q += " ORDER BY priority, id LIMIT 1"
        row = c.execute(q, args).fetchone()
        if not row:
            return None
        c.execute("UPDATE jobs SET status='running', started_at=?, attempts=attempts+1 WHERE id=?", (now, row["id"]))
        return c.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()


def finish_job(job_id: int, result: Any = None) -> None:
    run("UPDATE jobs SET status='done', finished_at=?, result_json=?, error=NULL WHERE id=?",
        time.time(), jdump(result), job_id)


def fail_job(job_id: int, error: str, retry_in: float | None = None, max_attempts: int = 3) -> None:
    row = one("SELECT attempts FROM jobs WHERE id=?", job_id)
    if retry_in is not None and row and row["attempts"] < max_attempts:
        run("UPDATE jobs SET status='queued', run_after=?, error=? WHERE id=?", time.time() + retry_in, error, job_id)
    else:
        run("UPDATE jobs SET status='failed', finished_at=?, error=? WHERE id=?", time.time(), error, job_id)


def requeue_stale(older_than: float = 900) -> None:
    """Jobs left 'running' by a crashed worker go back to the queue."""
    run("UPDATE jobs SET status='queued' WHERE status='running' AND started_at < ?", time.time() - older_than)
