"""Server blacklist: auto entries (3-day expiry, written by
experiment.py) and manual entries (never expire, via this CLI).
Read-only queries work on any connection, including mode=ro."""

import argparse
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS blacklist (
    id INTEGER PRIMARY KEY,
    server_id INTEGER NOT NULL,
    reason TEXT NOT NULL,
    source TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    added_at TEXT NOT NULL,
    expires_at TEXT,
    removed_at TEXT
);
"""

AUTO_TTL_DAYS = 3


def _utcnow():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.isoformat() if isinstance(dt, datetime) else dt


def initialize(db):
    db.executescript(SCHEMA)
    db.commit()


def _has_table(db):
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table'"
        " AND name='blacklist'").fetchone() is not None


def _active_auto_exists(db, server_id, now):
    if not _has_table(db):
        return False
    return db.execute(
        "SELECT 1 FROM blacklist WHERE server_id=? AND source='auto'"
        " AND removed_at IS NULL"
        " AND (expires_at IS NULL OR expires_at>?)",
        (server_id, _iso(now))).fetchone() is not None


def add(db, server_id, reason, source, detail=None, ttl_days=None,
        now=None):
    """Insert a blacklist entry; returns the new id or None when an
    active auto entry already covers the server (auto adds only)."""
    now = now or _utcnow()
    if source not in ("auto", "manual"):
        raise ValueError("source must be auto or manual")
    if server_id <= 0 or not reason.strip():
        raise ValueError("positive server id and reason required")
    if source == "auto":
        if _active_auto_exists(db, server_id, now):
            return None
        ttl_days = AUTO_TTL_DAYS if ttl_days is None else ttl_days
        if ttl_days <= 0:
            raise ValueError("ttl_days must be positive")
        expires_at = now + timedelta(days=ttl_days)
    else:
        expires_at = None
    with db:
        cur = db.execute(
            "INSERT INTO blacklist(server_id, reason, source,"
            " detail_json, added_at, expires_at)"
            " VALUES (?,?,?,?,?,?)",
            (server_id, reason, source, json.dumps(detail or {}),
             _iso(now), _iso(expires_at)))
    return cur.lastrowid


def remove(db, server_id, now=None):
    """Mark all active entries for the server removed."""
    now = now or _utcnow()
    with db:
        cur = db.execute(
            "UPDATE blacklist SET removed_at=? WHERE server_id=?"
            " AND removed_at IS NULL AND (expires_at IS NULL OR expires_at>?)",
            (_iso(now), server_id, _iso(now)))
    return cur.rowcount


def active(db, now=None):
    """{server_id: entry} for live entries; {} when the table is
    missing. Safe on read-only connections (SELECT only)."""
    now = now or _utcnow()
    if not _has_table(db):
        return {}
    result = {}
    cursor = db.execute(
            "SELECT * FROM blacklist WHERE removed_at IS NULL"
            " AND (expires_at IS NULL OR expires_at>?)"
            " ORDER BY source='manual', id", (_iso(now),))
    names = [column[0] for column in cursor.description]
    for values in cursor:
        row = dict(zip(names, values))
        try:
            detail = json.loads(row["detail_json"] or "{}")
        except ValueError:
            detail = {}
        result[row["server_id"]] = {
            "reason": row["reason"], "source": row["source"],
            "added_at": row["added_at"], "expires_at": row["expires_at"],
            "detail": detail}
    return result


def _open_ro(path):
    path = os.path.abspath(os.path.expanduser(path))
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def _open_rw(path):
    import market
    db = market.connect_db(os.path.expanduser(path))
    initialize(db)
    return db


def main(argv=None):
    from cli import DEFAULT_DB
    parser = argparse.ArgumentParser(description="Server blacklist")
    parser.add_argument("--db", default=DEFAULT_DB, help="SQLite path")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_list = sub.add_parser("list", help="show active entries")
    p_list.add_argument("--all", action="store_true",
                        help="include expired/removed entries")
    p_add = sub.add_parser("add", help="manual entry (never expires)")
    p_add.add_argument("--server", type=int, required=True)
    p_add.add_argument("--reason", required=True)
    p_rm = sub.add_parser("rm", help="remove active entries")
    p_rm.add_argument("--server", type=int, required=True)
    for subparser in (p_list, p_add, p_rm):
        subparser.add_argument("--db", default=argparse.SUPPRESS,
                               help="SQLite path")
    args = parser.parse_args(argv)

    if args.cmd == "list":
        db = _open_ro(args.db)
        try:
            if not _has_table(db):
                print("no blacklist table")
                return 0
            if args.all:
                rows = db.execute("SELECT * FROM blacklist ORDER BY id")
            else:
                rows = db.execute(
                    "SELECT * FROM blacklist WHERE removed_at IS NULL"
                    " AND (expires_at IS NULL OR expires_at>?)"
                    " ORDER BY id", (_utcnow().isoformat(),))
            for row in rows:
                extra = ""
                if row["removed_at"]:
                    extra = f"  removed {row['removed_at']}"
                elif row["expires_at"] and \
                        row["expires_at"] <= _utcnow().isoformat():
                    extra = f"  expired {row['expires_at']}"
                print(f"#{row['id']} server {row['server_id']} "
                      f"{row['reason']} ({row['source']}) "
                      f"added {row['added_at']} "
                      f"expires {row['expires_at'] or 'never'}{extra}")
        finally:
            db.close()
        return 0
    db = _open_rw(args.db)
    try:
        if args.cmd == "add":
            bid = add(db, args.server, args.reason, "manual")
            print(f"blacklisted {args.server}: {args.reason} (#{bid},"
                  " manual, no expiry)")
        elif args.cmd == "rm":
            n = remove(db, args.server)
            print(f"removed {n} active entr{'y' if n == 1 else 'ies'}"
                  f" for {args.server}")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
