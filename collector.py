#!/usr/bin/env python3
"""Opt-in controls for a transient systemd --user scanner service.

`start` launches `cli.py scan --live --watch` inside a transient
systemd-run user service (no sudo, no unit file, no boot autostart).
`status`/`logs` are read-only; `stop` is an explicit user action.
No orders are ever placed and no credential values are passed: the
service reads the existing protected env file itself.
"""

import argparse
import math
import os
import subprocess
import sys

import collector_service
import scanner_watch
import spot
from cli import DEFAULT_DB, DEFAULT_ENV

UNIT = collector_service.UNIT
QUERY = ["systemctl", "--user", "show", UNIT,
         "--property=LoadState", "--property=ActiveState",
         "--property=Description", "--no-pager"]
STOP = ["systemctl", "--user", "stop", UNIT]
LOGS = ["journalctl", f"--user-unit={UNIT}", "--no-pager", "-n", "50"]


def err(msg):
    print(f"error: {msg}", file=sys.stderr)


def query_unit():
    """Query the unit once. Returns (props|None, error|None).

    props is a dict of LoadState/ActiveState/Description when the user
    manager answered; LoadState 'not-found' means no such unit. error
    covers a missing systemctl, no user bus, or a failed query."""
    try:
        cp = subprocess.run(QUERY, capture_output=True, text=True,
                            timeout=15)
    except FileNotFoundError:
        return None, "systemctl not found"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"cannot query systemd user manager: {exc}"
    if cp.returncode != 0:
        detail = cp.stderr.strip() or "systemd user manager unreachable"
        return None, f"cannot query systemd user manager: {detail}"
    props = {}
    for line in cp.stdout.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            props[k] = v
    if not props:
        return None, "systemd user manager gave an empty response"
    return props, None


def cmd_status(_args):
    props, error = query_unit()
    if error:
        err(error)
        return 1
    if props.get("LoadState") == "not-found":
        print(f"{UNIT}: not running (no such transient unit)")
        return 0
    print(f"{UNIT}: load={props.get('LoadState', '?')} "
          f"active={props.get('ActiveState', '?')}")
    return 0


def cmd_start(args):
    interval = (args.interval if args.interval is not None
                else scanner_watch.DEFAULT_INTERVAL)
    if not math.isfinite(interval) \
            or interval < scanner_watch.MIN_INTERVAL:
        err(f"--interval must be finite and "
            f">= {scanner_watch.MIN_INTERVAL:g} seconds")
        return 2
    db_path = os.path.expanduser(args.db)
    if db_path == ":memory:":
        err("--db :memory: would discard every scan")
        return 2
    env_file = os.path.expanduser(args.env_file)
    if not os.path.isfile(env_file):
        err(f"env file not found: {env_file}")
        return 2
    if not 0 <= args.spot_limit <= spot.MAX_SPOT_LIMIT:
        err(f"--spot-limit must be 0..{spot.MAX_SPOT_LIMIT}")
        return 2
    if args.tick_interval is not None and (
            not math.isfinite(args.tick_interval)
            or args.tick_interval < 10):
        err("--tick-interval must be finite and >= 10 seconds")
        return 2
    if args.spot_watch_interval is not None and (
            not math.isfinite(args.spot_watch_interval)
            or (args.spot_watch_interval != 0
                and args.spot_watch_interval < 10)):
        err("--spot-watch-interval must be finite, 0, or >= 10 seconds")
        return 2

    props, error = query_unit()
    if error:
        err(error)
        return 1
    if props.get("LoadState") != "not-found":
        desc = props.get("Description", "")
        active = props.get("ActiveState", "")
        if desc != collector_service.DESCRIPTION:
            err(f"{UNIT} exists but is not ours "
                f"(Description={desc!r}); refusing to touch it")
            return 2
        if active in ("active", "activating"):
            print(f"{UNIT} already running (parameters unchanged; "
                  f"stop it first to change "
                  f"--interval/--db/--spot-limit)")
            return 0
        err(f"{UNIT} exists but is {active or 'inactive'}; "
            f"run `python3 collector.py status` for details or "
            f"`python3 collector.py stop` to clear it")
        return 2

    argv = collector_service.start_command(
        db_path, env_file, interval, spot_limit=args.spot_limit,
        tick_interval=args.tick_interval,
        spot_watch_interval=args.spot_watch_interval)
    try:
        cp = subprocess.run(argv, capture_output=True, text=True,
                            timeout=30)
    except FileNotFoundError:
        err("systemd-run not found")
        return 1
    except (OSError, subprocess.TimeoutExpired) as exc:
        err(f"cannot launch {UNIT}: {exc}")
        return 1
    if cp.returncode != 0:
        err(f"systemd-run failed: {cp.stderr.strip() or cp.returncode}")
        return 1
    # Never claim success from systemd-run alone: re-query the unit.
    props, error = query_unit()
    if error:
        err(f"launch reported success but status query failed: {error}")
        return 1
    if props.get("ActiveState") in ("active", "activating"):
        print(f"{UNIT} started (interval {interval:g}s, "
              f"db {os.path.abspath(db_path)}); "
              f"read-only scans, no orders. Stop: collector.py stop")
        return 0
    err(f"{UNIT} did not stay up "
        f"(state {props.get('ActiveState', '?')}); "
        f"check `python3 collector.py logs`")
    return 1


def cmd_stop(_args):
    props, error = query_unit()
    if error:
        err(error)
        return 1
    if props.get("LoadState") == "not-found":
        print(f"{UNIT}: not running")
        return 0
    if props.get("Description", "") != collector_service.DESCRIPTION:
        err(f"{UNIT} is not ours "
            f"(Description={props.get('Description')!r}); "
            "refusing to stop it")
        return 2
    try:
        cp = subprocess.run(STOP, capture_output=True, text=True,
                            timeout=15)
    except FileNotFoundError:
        err("systemctl not found")
        return 1
    except (OSError, subprocess.TimeoutExpired) as exc:
        err(f"stop failed: {exc}")
        return 1
    if cp.returncode != 0:
        err(f"stop failed: {cp.stderr.strip() or cp.returncode}")
        return 1
    print(f"{UNIT} stopped")
    return 0


def cmd_logs(_args):
    try:
        cp = subprocess.run(LOGS, timeout=30)
    except FileNotFoundError:
        err("journalctl not found")
        return 1
    except (OSError, subprocess.TimeoutExpired) as exc:
        err(f"cannot read logs: {exc}")
        return 1
    return cp.returncode


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Opt-in control for the transient read-only scanner "
                    "service (systemd --user; no installs, no orders).")
    sub = parser.add_subparsers(dest="action", required=True)
    sp = sub.add_parser("start", help="start the transient scanner service")
    sp.add_argument("--db", default=DEFAULT_DB, help="SQLite path")
    sp.add_argument("--env-file", default=DEFAULT_ENV,
                    help="secrets file the service reads itself")
    sp.add_argument("--interval", type=float, default=None,
                    help="seconds between scans "
                    f"(finite, >= {scanner_watch.MIN_INTERVAL:g}, "
                    f"default {scanner_watch.DEFAULT_INTERVAL:g})")
    sp.add_argument("--tick-interval", type=float, default=None,
                    help="seconds between telemetry ticks "
                    "(finite, >= 10, default 30)")
    sp.add_argument("--spot-limit", type=int,
                    default=spot.DEFAULT_SPOT_LIMIT,
                    help="also fetch up to N spot floors per cycle "
                    f"(0..{spot.MAX_SPOT_LIMIT}, observation only)")
    sp.add_argument("--spot-watch-interval", type=float, default=None,
                    help="seconds between spot-rental inventory polls "
                    "(0 disables; finite, >= 10 otherwise)")
    sub.add_parser("stop", help="stop the service")
    sub.add_parser("status", help="show unit load/active state")
    sub.add_parser("logs", help="show last 50 journal lines")
    args = parser.parse_args(argv)
    return {"start": cmd_start, "stop": cmd_stop,
            "status": cmd_status, "logs": cmd_logs}[args.action](args)


if __name__ == "__main__":
    raise SystemExit(main())
