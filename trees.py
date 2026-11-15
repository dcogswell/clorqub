"""Jetski tree-ID transitions parsed from archived miner logs.

The pool assigns a work ID ("tree") per connection; the miner logs
`E:<epoch> | ID: <id> | SEED: <seed>` whenever its work changes. Share
rate follows the ID, so tracking transitions lets us line share bursts
up with tree changes (see PLAN.md "Benchmarking and auto-rental").
"""

import re

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
TREE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}T[0-9:.]+[+-]\d{2}:?\d{2})\s+"
    r"QUBJETSKI\b.*?E:(?P<epoch>\d+)\s*\|\s*ID:\s*(?P<tree_id>\S+)"
    r"\s*\|\s*SEED:\s*(?P<seed>\S+)")


def parse_tree_events(text):
    """Ordered tree-change events: {ts, epoch, tree_id, seed}.

    ts is the log line's leading ISO timestamp (kept as a string).
    Consecutive lines repeating the same tree id collapse to one
    event -- only actual transitions are emitted."""
    text = ANSI_RE.sub("", text or "").replace("\r", "\n")
    events = []
    for line in text.splitlines():
        match = TREE_RE.search(line)
        if match is None:
            continue
        tree_id = match.group("tree_id")
        if events and events[-1]["tree_id"] == tree_id:
            continue
        events.append({
            "ts": match.group("ts"),
            "epoch": int(match.group("epoch")),
            "tree_id": tree_id,
            "seed": match.group("seed"),
        })
    return events


def _chunk_texts(db, order_id, max_chunks=200):
    rows = db.execute(
        "SELECT chunk FROM rig_log_chunks WHERE order_id=? "
        "ORDER BY id DESC LIMIT ?",
        (order_id, max_chunks)).fetchall()
    return [row["chunk"] for row in reversed(rows)]


def current_tree(db, order_id):
    """The last tree event in the order's archived log, or None."""
    events = parse_tree_events("".join(_chunk_texts(db, order_id)))
    return events[-1] if events else None


def tree_segments(db, order_id):
    """[{tree_id, seed, epoch, start_ts, end_ts}] -- end_ts is the next
    segment's start, or None for the last (still-open) segment."""
    events = parse_tree_events("".join(_chunk_texts(db, order_id)))
    segments = []
    for index, event in enumerate(events):
        segments.append({
            "tree_id": event["tree_id"], "seed": event["seed"],
            "epoch": event["epoch"], "start_ts": event["ts"],
            "end_ts": events[index + 1]["ts"]
            if index + 1 < len(events) else None,
        })
    return segments
