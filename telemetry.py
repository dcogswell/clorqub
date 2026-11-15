#!/usr/bin/env python3
"""Read-only SSH polling of explicitly enrolled rigs; never controls miners."""

import argparse
import os
import sqlite3
import threading
from datetime import datetime, timezone

import fleet_store
import rig_logs
import rig_ssh
from market import connect_db
from orders import read_inventory
from telemetry_policy import POLL_SECONDS, sanitize_status, timestamp

DEFAULT_DB = "~/.local/share/clorqub/clorqub.sqlite3"
INVENTORY_MAX_AGE_SECONDS = 900


def _fresh_order_ids(inventory):
    """Managed-order ids from the latest good inventory; None if not fresh."""
    if not inventory.get("ok") or not inventory.get("captured_at"):
        return None
    try:
        captured = timestamp(inventory["captured_at"])
    except (TypeError, ValueError):
        return None
    age = (datetime.now(timezone.utc) - captured).total_seconds()
    if not -60 <= age <= INVENTORY_MAX_AGE_SECONDS:
        return None
    return {item["order_id"] for item in inventory.get("orders") or []
            if isinstance(item.get("order_id"), int)}


def _archive_log(db, row, pool):
    """Append the incremental log slice and advance the cursor."""
    offset = fleet_store.get_log_cursor(db, row["order_id"], pool)
    result = rig_logs.fetch_log_chunk(
        row["ssh_host"], row["ssh_port"], row["ssh_key_path"],
        row["order_id"], pool, offset)
    size = result["remote_size"]
    chunk = result["chunk"]
    nbytes = len(chunk.encode("utf-8"))
    if size <= offset:
        new_offset = size  # unchanged, or shrunk/rotated back
        start = max(0, size - nbytes)
    else:
        new_offset = offset + nbytes  # maybe < size when capped by max_bytes
        start = offset
    if chunk:
        fleet_store.append_log_chunk(
            db, row["order_id"], pool, start, chunk, new_offset)
    elif new_offset != offset:
        fleet_store.set_log_cursor(db, row["order_id"], pool, new_offset)


def collect_once(db_path, stop_event=None):
    db = connect_db(os.path.expanduser(db_path))
    try:
        fleet_store.initialize(db)
        live = _fresh_order_ids(read_inventory(db))
        for row in fleet_store.read_fleet(db):
            if stop_event is not None and stop_event.is_set():
                break
            if row["provision_state"] in ("cancelled", "missing") \
                    or not row["ssh_host"] or not row["ssh_port"]:
                continue
            try:
                raw = rig_ssh.ssh_json(
                    row["ssh_host"], row["ssh_port"], row["ssh_key_path"], row["order_id"])
                expected = row["alias"] if row["provision_state"] != "observe-only" else None
                safe = sanitize_status(raw, expected_alias=expected)
                fleet_store.append_observation(db, row["order_id"], safe)
            except (rig_ssh.SSHError, ValueError, OSError) as exc:
                # Never retain transport stderr, raw remote JSON or exception text.
                fleet_store.append_observation(db, row["order_id"], None, type(exc).__name__)
                # Order gone from a fresh good inventory + SSH failing:
                # stop polling user-cancelled rigs.
                if isinstance(exc, rig_ssh.SSHError) and live is not None \
                        and row["order_id"] not in live \
                        and row["provision_state"] != "observe-only":
                    fleet_store.set_state(db, row["order_id"], "missing")
                continue
            try:
                pool = safe["pool"] if safe.get("pool") in rig_logs.POOLS \
                    else row["pool"]
                _archive_log(db, row, pool)
            except (rig_ssh.SSHError, ValueError, OSError):
                pass  # a log failure must not fail the status poll
    finally:
        db.close()
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--watch", action="store_true")
    args = parser.parse_args(argv)
    stop = threading.Event()
    try:
        while True:
            collect_once(args.db, stop)
            if not args.watch or stop.wait(POLL_SECONDS):
                break
    except KeyboardInterrupt:
        return 0
    except (sqlite3.Error, OSError):
        parser.exit(2, "Cannot store telemetry; check --db and directory permissions.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
