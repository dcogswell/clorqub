#!/usr/bin/env python3
"""Tree scout: report the pool's current tree assignment to clorqub.

Run this on any always-on machine mining the same pool+wallet (e.g. a
home PC). It tails a miner log -- or reads piped miner stdout -- and
POSTs the current tree to the clorqub API. The fleet then knows which
tree a new rental would draw *before paying for one*: dead-tree landings
cancel free, and probe fills defer while the current offering is dead.

  # tail an existing log file (e.g. from a systemd unit or nohup):
  tree_scout.py --api http://HOST:PORT --file /path/to/miner.log

  # or pipe the miner's own stdout:
  jetski-miner 2>&1 | tree_scout.py --api http://HOST:PORT --stdin

The API is usually bound to localhost; over the WAN run the scout
through an SSH tunnel:

  ssh -N -L 8420:localhost:8420 clorqub-host &
  tree_scout.py --api http://localhost:8420 --file miner.log

Posts one report per tree transition plus a heartbeat (--interval,
default 120s) carrying the latest shares/it-s, so a silent tree run is
also dead-tree evidence. Reports queue locally for --queue seconds if
the API is unreachable (restarts, network blips) and flush in order.
"""

import argparse
import json
import os
import re
import socket
import sys
import time
import urllib.request
from collections import deque
from datetime import datetime, timezone

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# 2026-11-19T11:02:16.975+00:00 QUBJETSKI ... E:240 | ID: X | SEED: Y
TREE_RE = re.compile(
    r"\bE:(?P<epoch>\d+)\s*\|\s*ID:\s*(?P<tree_id>\S+)"
    r"\s*\|\s*SEED:\s*(?P<seed>\S+)")
# ... E:240 | SHARES: 1123/1123 (R:0) | [AVX512] 48 it/s | [CUDA] 456
# it/s | 496 avg it/s
STAT_RE = re.compile(
    r"\bE:(?P<epoch>\d+)\s*\|\s*SHARES:\s*(?P<shares>\d+)\s*/"
    r"(?P<total>\d+)\s*\(R:(?P<rej>\d+)\).*?"
    r"(?P<its>\d+)\s*avg it/s")


def _post(api, payload, timeout=10):
    req = urllib.request.Request(
        api.rstrip("/") + "/api/tree/report",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status < 400
    except (urllib.error.URLError, OSError, ValueError):
        return False


def feed(raw, state, api, queue):
    """One log line in: update state, POST on tree transitions."""
    line = ANSI_RE.sub("", raw or "")
    m = TREE_RE.search(line)
    if m is not None:
        tid = m.group("tree_id")
        if state["tree_id"] != tid:
            state.update(tree_id=tid,
                         epoch=int(m.group("epoch")),
                         seed=m.group("seed"))
            flush(state, api, queue, reason="tree")
    s = STAT_RE.search(line)
    if s is not None:
        state["its"] = float(s.group("its"))
        state["shares"] = int(s.group("shares"))
        state["rejected"] = int(s.group("rej"))
        if state["epoch"] is None:
            state["epoch"] = int(s.group("epoch"))


def flush(state, api, queue, reason):
    payload = {
        "source": state["source"],
        "tree_id": state["tree_id"],
        "epoch": state["epoch"],
        "seed": state["seed"],
        "its": state["its"],
        "shares": state["shares"],
        "detail": {"reason": reason,
                   "rejected": state["rejected"]}}
    if not _post(api, payload):
        queue.append((time.time(), payload, reason))


def drain(api, queue):
    while queue and _post(api, queue[0][1]):
        queue.popleft()


def tail(path, poll=1.0):
    """Yield lines appended to `path`, surviving truncation/rotation."""
    f = None
    inode = None
    while True:
        try:
            st = os.stat(path)
            if inode != st.st_ino:
                if f is not None:
                    f.close()
                f = open(path, errors="replace")
                f.seek(0, os.SEEK_END)
                inode = st.st_ino
            line = f.readline()
        except FileNotFoundError:
            line = ""
            if f is not None:
                f.close()
            f = inode = None
        if not line:
            time.sleep(poll)
            continue
        yield line


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--api", required=True,
                    help="clorqub API base URL, e.g. http://host:8420")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--file", help="miner log file to tail")
    src.add_argument("--stdin", action="store_true",
                     help="read piped miner stdout")
    ap.add_argument("--source", default=socket.gethostname(),
                    help="name this scout reports as")
    ap.add_argument("--interval", type=float, default=120,
                    help="heartbeat report interval seconds")
    ap.add_argument("--queue", type=float, default=3600,
                    help="seconds to hold reports for a down API")
    args = ap.parse_args()

    state = {"source": args.source, "tree_id": None, "epoch": None,
             "seed": None, "its": None, "shares": None, "rejected": 0}
    queue = deque()
    last_flush = last_drain = time.time()
    lines = tail(args.file) if args.file else iter(sys.stdin)
    for line in lines:
        feed(line, state, args.api, queue)
        now = time.time()
        if now - last_drain >= 30:
            drain(args.api, queue)
            last_drain = now
        if state["tree_id"] and now - last_flush >= args.interval:
            flush(state, args.api, queue, reason="heartbeat")
            last_flush = now
        while queue and now - queue[0][0] > args.queue:
            queue.popleft()


if __name__ == "__main__":
    main()
