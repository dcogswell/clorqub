#!/usr/bin/env python3
"""Read-only clorqub marketplace CLI.

Shortlists Clore listings against user-supplied jetski yield anchors.
Makes no remote changes (no rent/cancel/bid calls); the only writes are
to the local SQLite db via --db.
"""

import argparse
import json
import math
import os
import signal
import sqlite3
import sys
import threading
import urllib.error
from datetime import datetime, timezone

import scanner_watch
import spot
import orders
import telemetry
from telemetry_policy import POLL_SECONDS
from formatting import rel_percent
from market import (
    connect_db,
    fetch_marketplace,
    ingest_marketplace,
    load_env_file,
    score_listing,
)


DEFAULT_DB = "~/.local/share/clorqub/clorqub.sqlite3"
DEFAULT_ENV = "~/.config/clorqub/env"

FETCH_ERRORS = (RuntimeError, urllib.error.URLError, ValueError, OSError)
INPUT_ERRORS = (OSError, json.JSONDecodeError)
PROCESS_ERRORS = (ValueError, KeyError, TypeError, AttributeError,
                  sqlite3.Error)


def err(msg):
    print(f"error: {msg}", file=sys.stderr)


def yield_marker(row):
    """Render the benchmark-anchored yield: '~' approximate, '>' lower bound."""
    sols = row["estimated_sols_hr"]
    if sols is None:
        return "n/a"
    if row.get("benchmark_kind") == "lower_bound":
        return f">{sols:.0f}"
    return f"~{sols:.0f}"


def spot_floor_label(row):
    """Fee-inclusive spot floor, '*' if offers seen; '-' missing/stale."""
    s = row.get("spot") or {}
    floor = s.get("fee_inclusive_floor_usd_day")
    if floor is None:
        return "-"
    return f"{floor:.2f}" + ("*" if s.get("offers_count") else "")


def display(rows, limit, source_desc, inventory=None, orders_ok=None):
    eligible = sorted(
        (row for row in rows if row["eligible"]),
        key=lambda row: row["estimated_sols_per_usd"],
        reverse=True,
    )
    print(f"Source: {source_desc}")
    print(
        "Pool: jetski only. Ranked: single-GPU RTX 3070 / 3080 Ti / 3090 Ti "
        "listings whose CPU matches the benchmarked models "
        "(Ryzen 9 7945HX / 7950X); every other listing is unscored."
    )
    print()
    print(
        f"{'ID':>7} {'GPU':<12} {'OD$/d':>7} {'SpotFlr':>8} "
        f"{'sols/h':>7} {'ODsol/$':>8} {'CC':>3} {'thr':>6} "
        f"{'CPU model':<30} {'ISA':<16} {'Rel%':>6}"
    )
    for row in eligible[:limit]:
        print(
            f"{row['server_id']:>7} {str(row['gpu'] or '?'):<12.12} "
            f"{row['effective_price_day_usd']:>7.2f} "
            f"{spot_floor_label(row):>8} {yield_marker(row):>7} "
            f"{row['estimated_sols_per_usd']:>8.1f} "
            f"{row['country'] or '--':>3} "
            f"{str(row['threads_listed'] or '?'):>6.6} "
            f"{str(row['cpu']):<30.30} "
            f"{row['cpu_isa']:<16.16} {rel_percent(row['reliability']):>6}"
        )
    print()
    print(
        f"{len(eligible)} benchmark-matched candidate(s) "
        f"from {len(rows)} listings "
        "(on-demand sols/$ is provisional; ranking is on-demand only)."
    )
    print(
        "OD$/d = on-demand fee-inclusive USD/day. SpotFlr = observed "
        "fee-inclusive spot floor (USD-Blockchain, 1.25% fee assumed); "
        "* = offers present, '-' = not collected/stale. Indicative only: "
        "not a winning-bid price or execution guarantee."
    )
    if inventory is not None:
        print()
        print("Orders (observe-only; may include web-UI rentals; "
              "native currency, no USD totals):")
        if orders_ok is False:
            print(f"  inventory STALE/unknown: "
                  f"{inventory.get('error') or 'last poll failed'}; "
                  "values below may be outdated.")
        items = inventory.get("orders") or []
        if orders_ok is True and not items:
            print("  no orders returned.")
        for o in items:
            kind = ("spot" if o.get("spot") is True else
                    "on-demand" if o.get("spot") is False else "unknown")
            cur = o.get("currency") or "?"
            print(
                f"  order {o['order_id']} server {o.get('server_id') or '?'} "
                f"{kind} online={o.get('online_reported')} "
                f"{o.get('price_day_native')} {cur}/day "
                f"spend {o.get('spend_reported_native')} {cur}")
        print("  Reported spend is cumulative and not epoch-verified; "
              "creation-fee quote is not proof of charge.")
    if not eligible:
        print("No candidates matched the current benchmark anchors.")
    uncertainty = next(
        (r["reference_uncertainty"] for r in eligible
         if r.get("reference_uncertainty")),
        None,
    )
    print()
    print("WARNINGS / LIMITATIONS")
    print(
        "- Provisional shortlist, not a rental approval: the sols/h "
        "column is a reference-rig anchor observed by the user "
        "(~160 sols/h approximate; >350 sols/h source lower bound). "
        "It describes the reference rig, not a guaranteed floor or "
        "expected rate for any candidate listing."
    )
    if uncertainty:
        print(f"- Benchmark uncertainty: {uncertainty}")
    else:
        print(
            "- The reference rig's CPU thread allocation, epoch, and "
            "trainer version are unknown; equal CPU allocations are NOT "
            "assumed on the listed rigs."
        )
    print(
        "- $/day includes an assumed 5% renter fee from prior observation "
        "(not freshly fetched); creation fee and boot opportunity cost "
        "are omitted."
    )
    print(
        "- sols/h is a whole-rig figure: CPU and GPU it/s are not "
        "additive, and there is no QUBIC conversion yet."
    )


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Scan and rank Clore listings (read-only).")
    parser.add_argument("command", choices=["scan"])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--snapshot",
        help="Read a saved Clore marketplace JSON response "
             "(a stored file; not fresh live availability)")
    source.add_argument(
        "--live", action="store_true", help="Fetch the live marketplace")
    parser.add_argument("--db", default=DEFAULT_DB, help="SQLite path")
    parser.add_argument("--env-file", default=DEFAULT_ENV,
                        help="Local secrets file (live mode)")
    parser.add_argument("--top", type=int, default=25,
                        help="Number of ranked listings to show")
    parser.add_argument(
        "--watch", action="store_true",
        help="Opt-in: keep scanning on --interval (read-only; no orders "
             "are placed). Requires --live.")
    parser.add_argument("--interval", type=float, default=None,
                        help="Seconds between watch-mode scans "
                        f"(finite, >= {scanner_watch.MIN_INTERVAL:g}, "
                        f"default {scanner_watch.DEFAULT_INTERVAL:g})")
    parser.add_argument("--tick-interval", type=float, default=None,
                        help="Watch mode: seconds between telemetry "
                        f"ticks (finite, >= 10, "
                        f"default {POLL_SECONDS:g})")
    parser.add_argument("--spot-limit", type=int,
                        default=spot.DEFAULT_SPOT_LIMIT,
                        help="Live mode: also fetch up to N spot floors "
                        f"per cycle (0..{spot.MAX_SPOT_LIMIT}, "
                        "observation only -- no bids placed)")
    args = parser.parse_args(argv)

    if args.top < 1:
        parser.error("--top must be at least 1")
    if args.watch and args.snapshot:
        parser.error("--watch only works with --live")
    if not 0 <= args.spot_limit <= spot.MAX_SPOT_LIMIT:
        parser.error(f"--spot-limit must be 0..{spot.MAX_SPOT_LIMIT}")
    if args.snapshot and args.spot_limit > 0:
        parser.error("--spot-limit only works with --live "
                     "(snapshots never call the API)")
    if args.interval is not None and not args.watch:
        parser.error("--interval only makes sense with --watch")
    if args.tick_interval is not None and not args.watch:
        parser.error("--tick-interval only makes sense with --watch")
    interval = (args.interval if args.interval is not None
                else scanner_watch.DEFAULT_INTERVAL)
    tick_interval = (args.tick_interval if args.tick_interval is not None
                     else POLL_SECONDS)
    if args.watch:
        if not math.isfinite(interval) \
                or interval < scanner_watch.MIN_INTERVAL:
            parser.error(
                f"--interval must be finite and "
                f">= {scanner_watch.MIN_INTERVAL:g} seconds")
        if not math.isfinite(tick_interval) or tick_interval < 10:
            parser.error("--tick-interval must be finite and >= 10 seconds")
        if os.path.expanduser(args.db) == ":memory:":
            parser.error("--watch with --db :memory: discards every scan")

    if not args.watch:
        return scan_once(args)

    stop = threading.Event()

    def _stop(*_a):
        stop.set()

    old_int = signal.signal(signal.SIGINT, _stop)
    old_term = signal.signal(signal.SIGTERM, _stop)
    def _telemetry_tick():
        # SSH reads on enrolled rigs only; no Clore API calls here.
        try:
            return telemetry.collect_once(os.path.expanduser(args.db),
                                          stop)
        except (sqlite3.Error, OSError) as exc:
            print(f"telemetry tick failed ({type(exc).__name__})",
                  file=sys.stderr, flush=True)
            return 2

    try:
        args.stop_event = stop
        return scanner_watch.run_watch(
            lambda: scan_once(args), interval, stop,
            tick=_telemetry_tick, tick_interval=tick_interval)
    finally:
        signal.signal(signal.SIGINT, old_int)
        signal.signal(signal.SIGTERM, old_term)


def scan_once(args):
    """Run one snapshot/live scan + ingest + display.

    Returns 0 on success, 1 on a transient fetch failure (watch retries),
    2 on config/storage/bad-input errors (watch stops). In watch mode the
    table is replaced by one flushed status line per scan."""
    imported_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if args.snapshot:
        path = os.path.abspath(os.path.expanduser(args.snapshot))
        try:
            with open(path, encoding="utf-8") as f:
                payload = json.load(f)
        except INPUT_ERRORS as exc:
            err(f"cannot read snapshot {path}: {exc}")
            return 2
        source_desc = (
            f"snapshot {path} imported at {imported_at} -- "
            "a saved file, not fresh live availability"
        )
        snapshot_source = f"snapshot:{path}"
    else:
        # A CLORE_API_KEY already in the process environment wins; the env
        # file is only consulted when no key was supplied, so an
        # inaccessible file cannot block a working key.
        api_key = os.environ.get("CLORE_API_KEY")
        if not api_key:
            try:
                env = load_env_file(os.path.expanduser(args.env_file))
            except OSError as exc:
                err(f"cannot read env file {args.env_file}: {exc}")
                return 2
            api_key = env.get("CLORE_API_KEY")
        if not api_key:
            err("CLORE_API_KEY is missing; set it in the process "
                "environment or the env file")
            return 2
        try:
            payload = fetch_marketplace(api_key)
        except FETCH_ERRORS as exc:
            # No exception text: a malformed-header error could echo
            # the API key. Class name + HTTP status only.
            status = getattr(exc, "code", None)
            detail = f" (HTTP {status})" if status else ""
            err(f"marketplace fetch failed: {type(exc).__name__}{detail}")
            return 1
        source_desc = f"live Clore API fetch imported at {imported_at}"
        snapshot_source = "clore_api"

    db_path = os.path.expanduser(args.db)
    if db_path != ":memory:":
        try:
            os.makedirs(os.path.dirname(os.path.abspath(db_path)),
                        exist_ok=True)
        except OSError as exc:
            err(f"cannot create db directory for {db_path}: {exc}")
            return 2
    try:
        db = connect_db(db_path)
    except sqlite3.Error as exc:
        err(f"cannot open db {db_path}: {exc}")
        return 2
    try:
        try:
            ingest_marketplace(db, payload, source=snapshot_source,
                               captured_at=imported_at)
            rows = [score_listing(server)
                    for server in payload["servers"]]
            inventory = None
            spot_attempts = 0
            orders_ok = None
            if not args.snapshot:
                # One bounded inventory request per live cycle; a failed
                # poll keeps the previous good snapshot and skips the
                # spot batch so an error never triggers extra requests.
                orders_ok = orders.refresh_inventory(
                    db, api_key, getattr(args, "stop_event", None))
                orders.refresh_wallets(
                    db, api_key, getattr(args, "stop_event", None))
                inventory = orders.read_inventory(db)
                if orders_ok and args.spot_limit > 0:
                    spot_attempts = spot.refresh_quotes(
                        db, payload["servers"], api_key,
                        args.spot_limit,
                        getattr(args, "stop_event", None))
                quotes = spot.latest_quotes(db)
                for row in rows:
                    row["spot"] = spot.summarize_quote(
                        quotes.get(row["server_id"]))
        except PROCESS_ERRORS as exc:
            err(f"failed to ingest/score listings: {exc}")
            return 2
    finally:
        db.close()
    if getattr(args, "watch", False):
        detail = ""
        if orders_ok is not None:
            detail += f", orders {'ok' if orders_ok else 'stale'}"
        if spot_attempts:
            detail += f", spot attempts {spot_attempts}"
        print(f"{imported_at} scan ok: {len(rows)} listings -> {db_path}"
              f"{detail}", flush=True)
    else:
        display(rows, args.top, source_desc, inventory, orders_ok)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
