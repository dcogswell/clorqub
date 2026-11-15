"""Durable operator rental records and append-only rig observations."""

import json

from market import utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS rental_attempts (
    attempt_id TEXT PRIMARY KEY,
    server_id INTEGER NOT NULL,
    alias TEXT NOT NULL,
    before_ids_json TEXT NOT NULL,
    quote_json TEXT NOT NULL,
    state TEXT NOT NULL,
    order_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fleet_rentals (
    order_id INTEGER PRIMARY KEY,
    server_id INTEGER NOT NULL,
    alias TEXT NOT NULL,
    pool TEXT NOT NULL,
    deadline_at TEXT,
    max_cost_usd TEXT,
    quoted_cost_usd TEXT,
    ssh_key_path TEXT NOT NULL,
    ssh_host TEXT,
    ssh_port INTEGER,
    provision_state TEXT NOT NULL,
    auto_renew INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rig_observations (
    id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES fleet_rentals(order_id),
    captured_at TEXT NOT NULL,
    status_json TEXT,
    error_kind TEXT
);
CREATE INDEX IF NOT EXISTS rig_observation_order ON rig_observations(order_id, id);
CREATE TABLE IF NOT EXISTS rig_log_chunks (
    id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES fleet_rentals(order_id),
    pool TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    start_offset INTEGER NOT NULL,
    end_offset INTEGER NOT NULL,
    chunk TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS rig_log_chunks_order ON rig_log_chunks(order_id, pool, id);
CREATE TABLE IF NOT EXISTS rig_log_cursors (
    order_id INTEGER NOT NULL,
    pool TEXT NOT NULL,
    byte_offset INTEGER NOT NULL,
    PRIMARY KEY(order_id, pool)
);
"""


def initialize(db):
    db.executescript(SCHEMA)
    # migration: fleet_rentals.auto_renew (nullable 0/1; NULL = observe-only
    # or a record predating the column)
    cols = {row[1] for row in db.execute("PRAGMA table_info(fleet_rentals)")}
    if "auto_renew" not in cols:
        db.execute("ALTER TABLE fleet_rentals ADD COLUMN auto_renew INTEGER")
        db.commit()


def begin_attempt(db, attempt_id, server_id, alias, before_ids, quote):
    # The payload contains deployment details; retain only non-secret quote terms.
    safe_quote = {key: quote.get(key) for key in (
        "cost_bound_usd", "hourly_cost_usd", "creation_fee_usd",
        "max_cost_usd", "hours", "alias", "cpu_threads", "bootstrap_commit",
        "pool", "ssh_key_path", "auto_renew",
    )}
    now = utcnow()
    with db:
        db.execute(
            "INSERT INTO rental_attempts VALUES (?, ?, ?, ?, ?, 'creating', NULL, ?, ?)",
            (attempt_id, server_id, alias, json.dumps(sorted(before_ids)),
             json.dumps(safe_quote, sort_keys=True), now, now),
        )


def finish_attempt(db, attempt_id, state, order_id=None):
    if state not in ("unknown", "matched", "no_order", "failed"):
        raise ValueError("Invalid rental attempt state")
    with db:
        db.execute(
            "UPDATE rental_attempts SET state=?, order_id=?, updated_at=? WHERE attempt_id=?",
            (state, order_id, utcnow(), attempt_id),
        )


def pending_attempts(db):
    return [dict(row) for row in db.execute(
        "SELECT * FROM rental_attempts WHERE state IN ('creating','unknown') ORDER BY created_at"
    )]


def register_rental(db, *, order_id, server_id, alias, pool, deadline_at,
                    max_cost_usd, quoted_cost_usd, ssh_key_path,
                    auto_renew=None):
    """auto_renew: None (observe-only/unknown) or bool; stored 0/1."""
    now = utcnow()
    flag = None if auto_renew is None else (1 if auto_renew is True else 0)
    with db:
        db.execute(
            "INSERT INTO fleet_rentals (order_id, server_id, alias, pool,"
            " deadline_at, max_cost_usd, quoted_cost_usd, ssh_key_path,"
            " ssh_host, ssh_port, provision_state, auto_renew,"
            " created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, 'pending', ?, ?, ?)",
            (order_id, server_id, alias, pool, deadline_at, max_cost_usd,
             quoted_cost_usd, ssh_key_path, flag, now, now),
        )


def set_connection(db, order_id, host, port):
    with db:
        db.execute(
            "UPDATE fleet_rentals SET ssh_host=?, ssh_port=?, updated_at=? WHERE order_id=?",
            (host, port, utcnow(), order_id),
        )


def set_state(db, order_id, state):
    if state not in ("pending", "ready", "provision_failed", "cancelled", "missing", "observe-only"):
        raise ValueError("Invalid provisioning state")
    with db:
        db.execute(
            "UPDATE fleet_rentals SET provision_state=?, updated_at=? WHERE order_id=?",
            (state, utcnow(), order_id),
        )


def set_auto_renew(db, order_id, flag):
    with db:
        db.execute(
            "UPDATE fleet_rentals SET auto_renew=?, updated_at=? WHERE order_id=?",
            (1 if flag else 0, utcnow(), order_id),
        )


def append_observation(db, order_id, status, error_kind=None):
    with db:
        db.execute(
            "INSERT INTO rig_observations(order_id,captured_at,status_json,error_kind) VALUES (?,?,?,?)",
            (order_id, utcnow(), json.dumps(status, sort_keys=True) if status is not None else None,
             error_kind),
        )


def get_log_cursor(db, order_id, pool):
    row = db.execute(
        "SELECT byte_offset FROM rig_log_cursors WHERE order_id=? AND pool=?",
        (order_id, pool)).fetchone()
    return int(row["byte_offset"]) if row else 0


def set_log_cursor(db, order_id, pool, byte_offset):
    with db:
        db.execute(
            "INSERT INTO rig_log_cursors VALUES (?, ?, ?) "
            "ON CONFLICT(order_id,pool) DO UPDATE SET byte_offset=excluded.byte_offset",
            (order_id, pool, byte_offset),
        )


def append_log_chunk(db, order_id, pool, start_offset, chunk, end_offset):
    """Store a fetched slice and advance the cursor to end_offset."""
    with db:
        db.execute(
            "INSERT INTO rig_log_chunks"
            "(order_id,pool,captured_at,start_offset,end_offset,chunk)"
            " VALUES (?,?,?,?,?,?)",
            (order_id, pool, utcnow(), start_offset, end_offset, chunk),
        )
        db.execute(
            "INSERT INTO rig_log_cursors VALUES (?, ?, ?) "
            "ON CONFLICT(order_id,pool) DO UPDATE SET byte_offset=excluded.byte_offset",
            (order_id, pool, end_offset),
        )


def read_log_archive(db, order_id, pool, max_chars=200000):
    """Concatenated archived chunks (tail `max_chars` kept) for a rig/pool."""
    rows = db.execute(
        "SELECT captured_at, chunk FROM rig_log_chunks"
        " WHERE order_id=? AND pool=? ORDER BY id",
        (order_id, pool)).fetchall()
    text = "".join(row["chunk"] for row in rows)
    if len(text) > max_chars:
        text = text[-max_chars:]
    return {
        "text": text,
        "last_captured_at": rows[-1]["captured_at"] if rows else None,
        "chunks": len(rows),
    }


def read_fleet(db):
    if not db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='fleet_rentals'"
    ).fetchone():
        return []
    rows = db.execute(
        "SELECT f.*, a.id AS observation_id, a.captured_at AS attempted_at, a.error_kind,"
        " g.captured_at AS observed_at, g.status_json"
        " FROM fleet_rentals f"
        " LEFT JOIN rig_observations a ON a.id=("
        " SELECT MAX(id) FROM rig_observations WHERE order_id=f.order_id)"
        " LEFT JOIN rig_observations g ON g.id=("
        " SELECT MAX(id) FROM rig_observations WHERE order_id=f.order_id AND status_json IS NOT NULL)"
        " ORDER BY f.order_id DESC"
    )
    result = []
    for row in rows:
        item = dict(row)
        raw_status = item.pop("status_json")
        item["status"] = json.loads(raw_status) if raw_status else None
        result.append(item)
    return result
