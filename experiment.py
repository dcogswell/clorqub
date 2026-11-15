"""Experiment-phase auto-rental runner (see PLAN.md "Benchmarking and
auto-rental").

A `phase` is a bounded experiment window with a USD budget. `run`
watches the phase's experiment rentals (which the collector observes
into rig_observations) and applies re-roll rules: start-up restarts,
fault cancels inside the free window, dry-tree restarts, cancel +
re-rent after x/y, and slot filling across hardware tiers. Dry-run is
the default; --live spends money and requires --confirm-budget.

Only orders recorded in experiment_rentals are ever acted on.
"""

import argparse
import fcntl
import json
import math
import os
import re
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import fleet_store
import market
import orders
import rent
import rental_policy
import rig_ssh
import trees
from cli import DEFAULT_DB, DEFAULT_ENV

SCHEMA = """
CREATE TABLE IF NOT EXISTS experiment_phases (
    phase_id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    budget_usd TEXT NOT NULL,
    reserve_usd TEXT NOT NULL,
    config_json TEXT NOT NULL,
    closed_at TEXT
);
CREATE TABLE IF NOT EXISTS experiment_rentals (
    order_id INTEGER PRIMARY KEY,
    phase_id INTEGER NOT NULL,
    server_id INTEGER NOT NULL,
    tier TEXT NOT NULL,
    reason TEXT NOT NULL,
    x_min INTEGER NOT NULL,
    y_min INTEGER NOT NULL,
    hourly_usd TEXT NOT NULL,
    creation_fee_usd TEXT NOT NULL,
    quoted_cost_usd TEXT NOT NULL,
    created_at TEXT NOT NULL,
    ended_at TEXT,
    end_reason TEXT
);
CREATE TABLE IF NOT EXISTS experiment_events (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    phase_id INTEGER NOT NULL,
    order_id INTEGER,
    server_id INTEGER,
    kind TEXT NOT NULL,
    detail_json TEXT NOT NULL
);
"""

ENDED_STATES = ("cancelled", "missing", "provision_failed")
POOL = "jetski"
ZEN_RE = re.compile(
    r"\bRyzen \d+ (?:7\d{2}[05]|9\d{2}[05])(?:X3D|X|HX3D|HX)?\b")
CANCEL_RETRY_MIN = 10

DEFAULT_CONFIG = {
    "max_rentals": 2,
    "hours": 6,
    "max_price_hr": "0.30",
    "x_min": [30, 60, 90],
    "y_min": [15, 30, 60],
    "startup_min": 8,
    "startup_restart_every_min": 2,
    "fault_check_min": 6,
    "free_window_min": 10,
    "dry_after_min": 15,
    "restart_cooldown_min": 10,
    "unreachable_min": 15,
    "rerent_grace_min": 60,
    "stale_obs_sec": 120,
    "stale_market_min": 15,
}


def _dt(value):
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


def _iso(value):
    return value.isoformat() if isinstance(value, datetime) else value


def _minutes(n):
    return timedelta(minutes=n)


def _dec(value, default="0"):
    return Decimal(str(value)) if value is not None else Decimal(default)


def utcnow():
    return datetime.now(timezone.utc).replace(microsecond=0)


def open_db(path):
    db = market.connect_db(os.path.expanduser(path))
    fleet_store.initialize(db)
    db.executescript(SCHEMA)
    db.commit()
    return db


def validate_config(cfg):
    cfg = dict(cfg)
    if cfg["max_rentals"] < 1 or \
            cfg["max_rentals"] > rental_policy.MAX_AUTO_RENTALS:
        raise ValueError(
            f"max_rentals must be 1..{rental_policy.MAX_AUTO_RENTALS}")
    if isinstance(cfg["hours"], bool) or not isinstance(cfg["hours"], int) \
            or not 1 <= cfg["hours"] <= rental_policy.MAX_HOURS:
        raise ValueError(
            f"hours must be 1..{rental_policy.MAX_HOURS}")
    if not cfg["x_min"] or not cfg["y_min"]:
        raise ValueError("x_min/y_min lists must be nonempty")
    _dec(cfg["max_price_hr"])
    return cfg


def open_phase(db, now):
    return db.execute(
        "SELECT * FROM experiment_phases WHERE closed_at IS NULL "
        "ORDER BY phase_id DESC LIMIT 1").fetchone()


def emit(db, ts, phase_id, order_id, server_id, kind, detail, say=print):
    """Append an experiment event and echo it as one stdout line."""
    detail_json = json.dumps(detail or {}, sort_keys=True)
    with db:
        db.execute(
            "INSERT INTO experiment_events"
            "(ts, phase_id, order_id, server_id, kind, detail_json)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (_iso(ts), phase_id, order_id, server_id, kind, detail_json))
    say(f"{_iso(ts)} {kind} order={order_id} server={server_id} "
        f"{detail_json}")


def share_arrivals(good_obs):
    """[(captured_at, count)] using shares.submitted as a cumulative
    counter that resets on miner restart: a rise counts the delta, a
    fall to >0 counts the full post-reset value, the first observation
    counts when >0."""
    arrivals = []
    prev = None
    for ts, status in good_obs:
        submitted = (status.get("shares") or {}).get("submitted")
        if not isinstance(submitted, (int, float)):
            continue
        if prev is None:
            if submitted > 0:
                arrivals.append((ts, submitted))
        elif submitted > prev:
            arrivals.append((ts, submitted - prev))
        elif submitted < prev and submitted > 0:
            arrivals.append((ts, submitted))
        prev = submitted
    return arrivals


def _its_total(status):
    its = status.get("its") or {}
    if its.get("total") is not None:
        return its["total"]
    return (its.get("cpu") or 0) + (its.get("gpu") or 0)


def _observations(db, order_id):
    good = []
    for row in db.execute(
            "SELECT captured_at, status_json FROM rig_observations "
            "WHERE order_id=? ORDER BY id", (order_id,)):
        if row["status_json"] is None:
            continue
        try:
            status = json.loads(row["status_json"])
        except ValueError:
            continue
        good.append((_dt(row["captured_at"]), status))
    return good


def _restart_events(db, phase_id, order_id):
    return [_dt(row["ts"]) for row in db.execute(
        "SELECT ts FROM experiment_events WHERE phase_id=? AND "
        "order_id=? AND kind='restart' ORDER BY id",
        (phase_id, order_id))]


def _loads_ok(raw):
    try:
        json.loads(raw)
        return True
    except ValueError:
        return False


def _rental_state(db, phase, row, now, cfg, listings):
    order_id = row["order_id"]
    fleet = db.execute(
        "SELECT * FROM fleet_rentals WHERE order_id=?",
        (order_id,)).fetchone()
    all_rows = [(_dt(r["captured_at"]), r["status_json"])
                for r in db.execute(
                    "SELECT captured_at, status_json FROM "
                    "rig_observations WHERE order_id=? ORDER BY id",
                    (order_id,))]
    good = [(ts, json.loads(raw)) for ts, raw in all_rows
            if raw is not None and _loads_ok(raw)]
    unreachable_window = now - _minutes(cfg["unreachable_min"])
    window_rows = [raw for ts, raw in all_rows if ts >= unreachable_window]
    last_good_at_tmp = good[-1][0] if good else None
    created = _dt(row["created_at"])
    recent_errors_only = bool(window_rows) and all(
        raw is None for raw in window_rows) and (
        last_good_at_tmp is not None
        and now - last_good_at_tmp >= _minutes(cfg["unreachable_min"])
        or last_good_at_tmp is None
        and now - created >= _minutes(cfg["unreachable_min"]))
    t0 = good[0][0] if good else None
    last_good_at = good[-1][0] if good else None
    last_status = good[-1][1] if good else {}
    arrivals = share_arrivals(good)
    last_share_at = arrivals[-1][0] if arrivals else None
    restarts = _restart_events(db, phase["phase_id"], order_id)
    quiet_since = last_share_at or t0
    reroll_start = None
    if quiet_since is not None:
        after = [ts for ts in restarts if ts >= quiet_since]
        reroll_start = after[0] if after else None
    listing = listings.get(row["server_id"])
    gpu_count = (len(listing.get("gpu_array") or [])
                 if listing is not None else None)
    hardware = last_status.get("hardware") or {}
    deadline = _dt(fleet["deadline_at"]) if fleet else None
    return {
        "order_id": order_id,
        "server_id": row["server_id"],
        "tier": row["tier"],
        "reason": row["reason"],
        "x_min": row["x_min"],
        "y_min": row["y_min"],
        "hourly_usd": _dec(row["hourly_usd"]),
        "creation_fee_usd": _dec(row["creation_fee_usd"]),
        "quoted_cost_usd": _dec(row["quoted_cost_usd"]),
        "created_at": created,
        "age": now - created,
        "deadline_at": deadline,
        "provision_state": fleet["provision_state"] if fleet else None,
        "ssh_host": fleet["ssh_host"] if fleet else None,
        "ssh_port": fleet["ssh_port"] if fleet else None,
        "ssh_key_path": fleet["ssh_key_path"] if fleet else None,
        "t0": t0,
        "last_good_at": last_good_at,
        "obs_age": (now - last_good_at) if last_good_at else None,
        "last_two_zero_its": len(good) >= 2 and all(
            _its_total(status) == 0 for _, status in good[-2:]),
        "its_total": _its_total(last_status) if good else None,
        "gpu_its": (last_status.get("its") or {}).get("gpu"),
        "gpu_count_seen": (len(hardware["gpus"])
                           if isinstance(hardware.get("gpus"), list)
                           else None),
        "listing_gpu_count": gpu_count,
        "recent_errors_only": recent_errors_only,
        "last_obs_at": all_rows[-1][0] if all_rows else None,
        "last_share_at": last_share_at,
        "share_arrivals": arrivals,
        "last_restart_at": restarts[-1] if restarts else None,
        "restarts": restarts,
        "reroll_start": reroll_start,
        "quiet_since": quiet_since,
        "good_obs": good,
    }


def pending_rerents(db, phase_id):
    """rerent_scheduled events with no later rent/rerent_expired for
    the same server in this phase, as {server_id, due_at, tier}."""
    pending = {}
    for row in db.execute(
            "SELECT kind, server_id, detail_json, ts FROM "
            "experiment_events WHERE phase_id=? ORDER BY id",
            (phase_id,)):
        sid = row["server_id"]
        if row["kind"] == "rerent_scheduled":
            detail = json.loads(row["detail_json"])
            pending[sid] = {"server_id": sid,
                            "due_at": _dt(detail["due_at"]),
                            "tier": detail.get("tier") or "other"}
        elif sid in pending and row["kind"] in (
                "rent", "rent_failed", "rerent_expired"):
            del pending[sid]
    return list(pending.values())


def excluded_servers(db, phase_id):
    """Servers cancelled for fault/unreachable or whose rent failed;
    never re-chosen this phase."""
    excluded = set()
    for row in db.execute(
            "SELECT server_id, end_reason FROM experiment_rentals "
            "WHERE phase_id=? AND ended_at IS NOT NULL", (phase_id,)):
        reason = row["end_reason"] or ""
        if reason.startswith("fault:") or reason == "unreachable":
            excluded.add(row["server_id"])
    for row in db.execute(
            "SELECT server_id FROM experiment_events WHERE phase_id=? "
            "AND kind='rent_failed' AND server_id IS NOT NULL",
            (phase_id,)):
        excluded.add(row["server_id"])
    return excluded


def latest_listings(db):
    """Latest marketplace snapshot: {server_id: listing} plus the
    snapshot's captured_at."""
    row = db.execute(
        "SELECT captured_at, raw_json FROM marketplace_snapshots "
        "ORDER BY id DESC LIMIT 1").fetchone()
    if row is None or not row["raw_json"]:
        return {}, None
    try:
        servers = json.loads(row["raw_json"]).get("servers") or []
    except ValueError:
        return {}, _dt(row["captured_at"])
    return {int(s["id"]): s for s in servers if s.get("id") is not None}, \
        _dt(row["captured_at"])


def server_listings(db):
    """servers.latest_json per server: the newest known listing."""
    result = {}
    if not db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='servers'").fetchone():
        return result
    for row in db.execute("SELECT server_id, latest_json FROM servers"):
        try:
            result[row["server_id"]] = json.loads(row["latest_json"])
        except (ValueError, TypeError):
            pass
    return result


def history_stats(db, known_listings):
    """Per-hardware history from all past fleet_rentals:
    coverage {(cpu, gpu): observed hours},
    cpu_prior {cpu: median its.cpu after the rental's first third},
    gpu_prior {gpu: median its.gpu ditto}."""
    coverage = {}
    cpu_samples = {}
    gpu_samples = {}
    for fleet in db.execute("SELECT * FROM fleet_rentals"):
        listing = known_listings.get(fleet["server_id"])
        if listing is None:
            continue
        cpu = (listing.get("specs") or {}).get("cpu") or "Unknown CPU"
        gpu = market.offered_gpu(listing)
        key = (cpu, gpu)
        good = _observations(db, fleet["order_id"])
        if not good:
            continue
        t0, last = good[0][0], good[-1][0]
        coverage[key] = coverage.get(key, 0.0) + \
            max(0.0, (last - t0).total_seconds() / 3600.0)
        cutoff = t0 + (last - t0) / 3
        for ts, status in good:
            if ts < cutoff:
                continue
            its = status.get("its") or {}
            if its.get("cpu") is not None:
                cpu_samples.setdefault(cpu, []).append(its["cpu"])
            if gpu is not None and its.get("gpu") is not None:
                gpu_samples.setdefault(gpu, []).append(its["gpu"])
    return {
        "coverage": coverage,
        "cpu_prior": {k: _median(v) for k, v in cpu_samples.items()},
        "gpu_prior": {k: _median(v) for k, v in gpu_samples.items()},
    }


def _median(values):
    values = sorted(values)
    if not values:
        return None
    mid = len(values) // 2
    if len(values) % 2:
        return values[mid]
    return (values[mid - 1] + values[mid]) / 2


def committed_usd(db, phase_id, now, cfg):
    """USD spoken for: the cost bound of each active rental, plus for
    ended ones hourly x duration (+ creation fee once it outlived the
    free window)."""
    total = Decimal(0)
    for row in db.execute(
            "SELECT * FROM experiment_rentals WHERE phase_id=?",
            (phase_id,)):
        if row["ended_at"] is None:
            total += _dec(row["quoted_cost_usd"])
            continue
        seconds = Decimal(str(
            (_dt(row["ended_at"]) - _dt(row["created_at"]))
            .total_seconds()))
        total += _dec(row["hourly_usd"]) * seconds / Decimal(3600)
        if seconds > Decimal(cfg["free_window_min"] * 60):
            total += _dec(row["creation_fee_usd"])
    return total


def build_state(db, phase, now, cfg, apply_endings=True, say=print):
    """Everything decide() needs, derived from the db. When
    apply_endings, rentals whose fleet row ended are closed out first
    (ended_at + end_reason + an `ended` event)."""
    phase_id = phase["phase_id"]
    listings, snapshot_at = latest_listings(db)
    rentals = []
    for row in db.execute(
            "SELECT * FROM experiment_rentals WHERE phase_id=? "
            "AND ended_at IS NULL ORDER BY order_id", (phase_id,)):
        state = _rental_state(db, phase, row, now, cfg, listings)
        ended_reason = None
        if state["provision_state"] in ENDED_STATES:
            ended_reason = state["provision_state"]
        elif state["deadline_at"] is not None \
                and state["deadline_at"] <= now:
            ended_reason = "term_end"
        if ended_reason is not None:
            if apply_endings:
                with db:
                    db.execute(
                        "UPDATE experiment_rentals SET ended_at=?, "
                        "end_reason=? WHERE order_id=?",
                        (_iso(now), ended_reason, row["order_id"]))
                emit(db, now, phase_id, row["order_id"],
                     state["server_id"], "ended",
                     {"reason": ended_reason}, say=say)
            continue
        rentals.append(state)
    last_obs = [r["last_obs_at"] for r in rentals
                if r["last_obs_at"] is not None]
    telemetry_age_s = ((now - max(last_obs)).total_seconds()
                       if last_obs else None)
    committed = committed_usd(db, phase_id, now, cfg)
    budget = _dec(phase["budget_usd"])
    scored = {}
    for sid, listing in listings.items():
        scored[sid] = market.score_listing(listing)
    return {
        "phase_id": phase_id,
        "phase_ends_at": _dt(phase["ends_at"]),
        "budget": budget,
        "reserve": _dec(phase["reserve_usd"]),
        "committed": committed,
        "remaining": budget - committed,
        "rentals": rentals,
        "pending_rerents": pending_rerents(db, phase_id),
        "excluded": excluded_servers(db, phase_id),
        "listings": listings,
        "scored": scored,
        "snapshot_at": snapshot_at,
        "telemetry_age_s": telemetry_age_s,
        "snapshot_age_s": ((now - snapshot_at).total_seconds()
                           if snapshot_at else None),
        "pending_attempts": bool(fleet_store.pending_attempts(db)),
        "stats": history_stats(db, server_listings(db)),
    }


def _fault_reason(rental):
    """Fault checks for the free window; returns a short tag or None."""
    if rental["last_two_zero_its"]:
        return "zero_its"
    if rental["listing_gpu_count"]:
        if not rental["gpu_its"]:
            return "gpu_its"
        if rental["gpu_count_seen"] is not None \
                and rental["gpu_count_seen"] != rental["listing_gpu_count"]:
            return "gpu_count"
    return None


def decide(now, state, cfg):
    """Pure: state -> ordered action dicts. One action per rental per
    cycle; restart/cancel actions survive global blocks, rent actions
    are replaced by `blocked` entries."""
    actions = []
    telemetry_stale = bool(state["rentals"]) and (
        state["telemetry_age_s"] is None
        or state["telemetry_age_s"] > cfg["stale_obs_sec"])
    for r in [] if telemetry_stale else state["rentals"]:
        t0 = r["t0"]
        obs_age = r["obs_age"]
        if r["recent_errors_only"]:
            actions.append({"type": "cancel", "order_id": r["order_id"],
                            "server_id": r["server_id"],
                            "tier": r["tier"],
                            "reason": "unreachable", "rerent_at": None})
            continue
        if t0 is None or obs_age is None \
                or obs_age > timedelta(seconds=cfg["stale_obs_sec"]):
            continue
        if _minutes(cfg["fault_check_min"]) <= r["age"] \
                < _minutes(cfg["free_window_min"]) and (
                r["last_restart_at"] is None or now - r[
                    "last_restart_at"] >= _minutes(
                        cfg["startup_restart_every_min"])):
            fault = _fault_reason(r)
            if fault:
                actions.append({
                    "type": "cancel", "order_id": r["order_id"],
                    "server_id": r["server_id"], "tier": r["tier"],
                    "reason": f"fault:{fault}", "rerent_at": None})
                continue
        if now - t0 < _minutes(cfg["startup_min"]):
            if r["last_share_at"] is None and now - (
                    r["last_restart_at"] or t0) >= _minutes(
                        cfg["startup_restart_every_min"]):
                actions.append({"type": "restart",
                                "order_id": r["order_id"],
                                "server_id": r["server_id"],
                                "reason": "startup"})
            continue
        quiet = r["quiet_since"]
        reroll = r["reroll_start"]
        if reroll is not None and now - reroll >= _minutes(r["x_min"]) \
                and (r["last_share_at"] is None
                     or r["last_share_at"] < reroll):
            actions.append({
                "type": "cancel", "order_id": r["order_id"],
                "server_id": r["server_id"], "tier": r["tier"],
                "reason": "dry_after_x",
                "rerent_at": _iso(now + _minutes(r["y_min"]))})
            continue
        if now - quiet >= _minutes(cfg["dry_after_min"]) and now - (
                r["last_restart_at"] or t0) >= _minutes(
                    cfg["restart_cooldown_min"]):
            actions.append({"type": "restart", "order_id": r["order_id"],
                            "server_id": r["server_id"], "reason": "dry"})

    blocks = []
    if telemetry_stale:
        blocks.append("telemetry_stale")
    if now >= state["phase_ends_at"]:
        blocks.append("phase")
    if state["snapshot_age_s"] is None \
            or state["snapshot_age_s"] > cfg["stale_market_min"] * 60:
        blocks.append("stale_market")
    if state["pending_attempts"]:
        blocks.append("pending")
    if state["remaining"] <= 0:
        blocks.append("budget")

    rent_actions = []
    if not blocks and now < state["phase_ends_at"]:
        active_ids = {r["server_id"] for r in state["rentals"]}
        chosen = []
        rerented_ids = set()
        # pending rerents due: re-rent the same server while it is
        # still listed, acceptable and in budget
        for pending in state["pending_rerents"]:
            due = pending["due_at"]
            if now > due + _minutes(cfg["rerent_grace_min"]):
                actions.append({"type": "rerent_expired",
                                "server_id": pending["server_id"]})
                continue
            if now < due:
                continue
            sid = pending["server_id"]
            if sid in state["excluded"]:
                continue
            row = state["scored"].get(sid)
            if row is not None and row["ineligible_reason"] in (
                    None, "no matching benchmark") \
                    and row["hourly_cost_usd"] is not None \
                    and row["hourly_cost_usd"] <= _dec(
                        cfg["max_price_hr"]):
                rent_actions.append({"type": "rent", "server_id": sid,
                                     "reason": "rerent",
                                     "tier": pending["tier"]})
                chosen.append({"server_id": sid,
                               "tier": pending["tier"]})
                rerented_ids.add(sid)
        # fill empty slots with the least-explored acceptable listings
        while len(state["rentals"]) + sum(
                1 for p in state["pending_rerents"]
                if p["server_id"] not in rerented_ids) \
                + len(chosen) < cfg["max_rentals"]:
            candidates = _candidates(state, cfg, active_ids, chosen)
            if not candidates:
                break
            pick = candidates[0]
            chosen.append(pick)
            rent_actions.append({"type": "rent",
                                 "server_id": pick["server_id"],
                                 "reason": "fill", "tier": pick["tier"]})
    if blocks:
        actions.extend({"type": "blocked", "reason": reason}
                       for reason in blocks)
    else:
        actions.extend(rent_actions)
    return actions


def _candidates(state, cfg, active_ids, chosen):
    """Acceptable listings minus active/pending/excluded, zen-tier
    preference, sorted by (coverage asc, -its/usd prior, hourly, id)."""
    pending_ids = {p["server_id"] for p in state["pending_rerents"]}
    chosen_ids = {c["server_id"] for c in chosen}
    zen_have = sum(1 for r in state["rentals"] if r["tier"] == "zen") + \
        sum(1 for c in chosen if c["tier"] == "zen")
    zen_want = math.ceil(cfg["max_rentals"] / 2) - zen_have
    pool = []
    for sid, row in state["scored"].items():
        if sid in active_ids or sid in pending_ids or sid in chosen_ids \
                or sid in state["excluded"]:
            continue
        if row["ineligible_reason"] not in (None, "no matching benchmark"):
            continue
        hourly = row["hourly_cost_usd"]
        if hourly is None or hourly > _dec(cfg["max_price_hr"]):
            continue
        cpu = row["cpu"]
        gpu = row["gpu"]
        coverage = state["stats"]["coverage"].get((cpu, gpu), 0.0)
        cpu_prior = state["stats"]["cpu_prior"].get(cpu)
        gpu_prior = state["stats"]["gpu_prior"].get(gpu)
        prior = ((cpu_prior + gpu_prior) / hourly
                 if cpu_prior is not None and gpu_prior is not None
                 and hourly else None)
        pool.append({
            "server_id": sid, "cpu": cpu, "gpu": gpu,
            "hourly": hourly, "prior": prior, "coverage": coverage,
            "tier": "zen" if ZEN_RE.search(cpu) else "other",
        })
    if zen_want > 0 and any(c["tier"] == "zen" for c in pool):
        pool = [c for c in pool if c["tier"] == "zen"]
    pool.sort(key=lambda c: (
        c["coverage"],
        -c["prior"] if c["prior"] is not None else float("inf"),
        c["hourly"], c["server_id"]))
    return pool


def _active_burn(db, phase_id, now):
    """Remaining USD burn of active rentals: hourly x hours to
    deadline (0 past it)."""
    need = Decimal(0)
    for row in db.execute(
            "SELECT e.hourly_usd, f.deadline_at FROM "
            "experiment_rentals e LEFT JOIN fleet_rentals f "
            "ON f.order_id=e.order_id WHERE e.phase_id=? "
            "AND e.ended_at IS NULL", (phase_id,)):
        deadline = _dt(row["deadline_at"])
        if deadline is None or deadline <= now:
            continue
        hours_left = Decimal(str(
            (deadline - now).total_seconds())) / Decimal(3600)
        need += _dec(row["hourly_usd"]) * hours_left
    return need


def _last_cancel_failed(db, phase_id, order_id):
    row = db.execute(
        "SELECT ts FROM experiment_events WHERE phase_id=? AND "
        "order_id=? AND kind='cancel_failed' ORDER BY id DESC LIMIT 1",
        (phase_id, order_id)).fetchone()
    return _dt(row["ts"]) if row else None


def execute(db, db_path, env_file, phase, cfg, actions, now, live,
            say=print):
    """Run the decided actions. Dry-run only records `dryrun` events
    and never touches SSH, the wallet API, quote, create or cancel."""
    phase_id = phase["phase_id"]
    rented = False
    for action in actions:
        kind = action["type"]
        if not live:
            if kind == "blocked":
                emit(db, now, phase_id, None, None, "blocked",
                     {"reason": action["reason"]}, say=say)
            elif kind == "rerent_expired":
                emit(db, now, phase_id, None, action["server_id"],
                     "rerent_expired", {"server_id": action["server_id"]},
                     say=say)
            else:
                emit(db, now, phase_id, action.get("order_id"),
                     action.get("server_id"), "dryrun", action, say=say)
            continue
        if kind == "blocked":
            emit(db, now, phase_id, None, None, "blocked",
                 {"reason": action["reason"]}, say=say)
        elif kind == "rerent_expired":
            emit(db, now, phase_id, None, action["server_id"],
                 "rerent_expired", {"server_id": action["server_id"]},
                 say=say)
        elif kind == "restart":
            _exec_restart(db, phase_id, action, now, say)
        elif kind == "cancel":
            _exec_cancel(db, db_path, env_file, phase_id, action, now,
                         say)
        elif kind == "rent":
            if rented:
                emit(db, now, phase_id, None, action["server_id"],
                     "blocked", {"reason": "one_rent_per_cycle"}, say=say)
                continue
            _exec_rent(db, db_path, env_file, phase, cfg, action, now,
                       say)
            rented = True


def _exec_restart(db, phase_id, action, now, say):
    order_id = action["order_id"]
    fleet = db.execute(
        "SELECT ssh_host, ssh_port, ssh_key_path FROM fleet_rentals "
        "WHERE order_id=?", (order_id,)).fetchone()
    before = trees.current_tree(db, order_id)
    detail = {"reason": action["reason"],
              "tree_id_before": before["tree_id"] if before else None}
    try:
        if fleet is None or not fleet["ssh_host"]:
            raise rig_ssh.SSHError("no ssh endpoint")
        detail["output"] = rig_ssh.restart_miner(
            fleet["ssh_host"], fleet["ssh_port"], fleet["ssh_key_path"],
            order_id, POOL)
    except rig_ssh.SSHError as exc:
        emit(db, now, phase_id, order_id, action["server_id"],
             "restart_failed", {**detail, "error": str(exc)}, say=say)
    else:
        emit(db, now, phase_id, order_id, action["server_id"],
             "restart", detail, say=say)


def _exec_cancel(db, db_path, env_file, phase_id, action, now, say):
    order_id = action["order_id"]
    failed_at = _last_cancel_failed(db, phase_id, order_id)
    if failed_at is not None \
            and now - failed_at < _minutes(CANCEL_RETRY_MIN):
        emit(db, now, phase_id, order_id, action["server_id"],
             "blocked", {"reason": "cancel_retry_backoff"}, say=say)
        return
    rc, info = rent.cancel_managed_order(order_id, db_path, env_file,
                                         say=say, warn=say)
    if rc == 0:
        with db:
            db.execute(
                "UPDATE experiment_rentals SET ended_at=?, end_reason=? "
                "WHERE order_id=?",
                (_iso(now), action["reason"], order_id))
        emit(db, now, phase_id, order_id, action["server_id"], "cancel",
             {"reason": action["reason"]}, say=say)
        if action.get("rerent_at"):
            emit(db, now, phase_id, order_id, action["server_id"],
                 "rerent_scheduled",
                 {"server_id": action["server_id"],
                  "tier": action.get("tier"),
                  "due_at": action["rerent_at"]}, say=say)
    else:
        emit(db, now, phase_id, order_id, action["server_id"],
             "cancel_failed", {"info": _safe_info(info)}, say=say)


def _safe_info(info):
    return {k: info.get(k) for k in ("order_id", "stage", "state",
                                     "provision_state")}


def _exec_rent(db, db_path, env_file, phase, cfg, action, now, say):
    phase_id = phase["phase_id"]
    sid = action["server_id"]
    committed = committed_usd(db, phase_id, now, cfg)
    remaining = _dec(phase["budget_usd"]) - committed
    if remaining <= 0:
        emit(db, now, phase_id, None, sid, "blocked",
             {"reason": "budget"}, say=say)
        return
    try:
        api_key, _ = rent.load_secrets(env_file, need_creds=False)
        wallets = orders.sanitize_wallets(orders.fetch_wallets(api_key))
    except Exception as exc:
        emit(db, now, phase_id, None, sid, "blocked",
             {"reason": f"balance: {type(exc).__name__}"}, say=say)
        return
    balance = next((_dec(w["balance"]) for w in wallets
                    if w["name"] == "USD-Blockchain"), None)
    estimate = None
    row = market.score_listing((latest_listings(db)[0] or {}).get(sid)
                               or {})
    if row["hourly_cost_usd"]:
        estimate = _dec(row["hourly_cost_usd"]) * cfg["hours"]
    need = _active_burn(db, phase_id, now) + _dec(phase["reserve_usd"]) \
        + (estimate or Decimal(0))
    if balance is None or balance < need:
        emit(db, now, phase_id, None, sid, "blocked",
             {"reason": "balance",
              "balance": str(balance), "need": str(need)}, say=say)
        return
    try:
        ctx = rent.quote_order(
            server_id=sid, hours=cfg["hours"],
            max_cost_usd=str(remaining.quantize(Decimal("0.01"))),
            pool=POOL, cpu_threads=None, db_path=db_path,
            env_file=env_file, key_path=rent.DEFAULT_KEY,
            auto_renew=False, say=say, warn=say)
    except Exception as exc:
        emit(db, now, phase_id, None, sid, "rent_failed",
             {"stage": "quote", "error": type(exc).__name__}, say=say)
        return
    bound = _dec(ctx["quote"]["cost_bound_usd"])
    if bound + committed > _dec(phase["budget_usd"]):
        emit(db, now, phase_id, None, sid, "blocked",
             {"reason": "budget", "bound": str(bound)}, say=say)
        return
    rc, info = rent.create_order_flow(ctx, say=say, warn=say)
    if rc != 0 or not info.get("order_id"):
        emit(db, now, phase_id, info.get("order_id"), sid,
             "rent_failed", {"info": _safe_info(info)}, say=say)
        return
    order_id = info["order_id"]
    count = db.execute(
        "SELECT COUNT(*) FROM experiment_rentals WHERE phase_id=?",
        (phase_id,)).fetchone()[0]
    x_min = cfg["x_min"][count % len(cfg["x_min"])]
    y_min = cfg["y_min"][count % len(cfg["y_min"])]
    quote = ctx["quote"]
    with db:
        db.execute(
            "INSERT INTO experiment_rentals(order_id, phase_id, "
            "server_id, tier, reason, x_min, y_min, hourly_usd, "
            "creation_fee_usd, quoted_cost_usd, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (order_id, phase_id, sid, action["tier"], action["reason"],
             x_min, y_min, str(quote["hourly_cost_usd"]),
             str(quote["creation_fee_usd"]),
             str(quote["cost_bound_usd"]), _iso(now)))
    emit(db, now, phase_id, order_id, sid, "rent",
         {"tier": action["tier"], "reason": action["reason"],
          "x_min": x_min, "y_min": y_min,
          "quoted_cost_usd": str(quote["cost_bound_usd"])}, say=say)


def run_cycle(db, db_path, env_file, phase, cfg, live, now=None,
              say=print):
    now = now or utcnow()
    state = build_state(db, phase, now, cfg, say=say)
    execute(db, db_path, env_file, phase, cfg,
            decide(now, state, cfg), now, live, say=say)


# ---------------------------------------------------------------- CLI

def _add_db_arg(parser):
    parser.add_argument("--db", default=DEFAULT_DB, help="SQLite path")


def _config_from_args(args):
    cfg = dict(DEFAULT_CONFIG)
    for key in ("max_rentals", "hours", "x_min", "y_min"):
        value = getattr(args, key, None)
        if value is not None:
            cfg[key] = value
    for key in ("max_price_hr", "startup_min",
                "startup_restart_every_min", "fault_check_min",
                "free_window_min", "dry_after_min",
                "restart_cooldown_min", "unreachable_min",
                "rerent_grace_min", "stale_obs_sec",
                "stale_market_min"):
        value = getattr(args, key, None)
        if value is not None:
            cfg[key] = value
    if isinstance(cfg["x_min"], str):
        cfg["x_min"] = [int(v) for v in cfg["x_min"].split(",")]
    if isinstance(cfg["y_min"], str):
        cfg["y_min"] = [int(v) for v in cfg["y_min"].split(",")]
    return validate_config(cfg)


def cmd_start_phase(args):
    db = open_db(args.db)
    try:
        now = utcnow()
        existing = db.execute(
            "SELECT phase_id, ends_at FROM experiment_phases WHERE "
            "closed_at IS NULL AND ends_at > ?",
            (_iso(now),)).fetchone()
        if existing is not None:
            print(f"phase {existing['phase_id']} is still open "
                  f"(ends {existing['ends_at']})", file=sys.stderr)
            return 2
        cfg = _config_from_args(args)
        budget = _dec(args.budget_usd)
        if budget <= 0:
            print("--budget-usd must be positive", file=sys.stderr)
            return 2
        ends_at = now + timedelta(hours=args.phase_hours)
        with db:
            db.execute(
                "INSERT INTO experiment_phases(started_at, ends_at, "
                "budget_usd, reserve_usd, config_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (_iso(now), _iso(ends_at), str(budget),
                 str(_dec(args.reserve_usd)),
                 json.dumps(cfg, sort_keys=True)))
        print(f"phase started: {_iso(now)} -> {_iso(ends_at)} "
              f"budget ${budget} reserve ${args.reserve_usd}")
        print(json.dumps(cfg, indent=2, sort_keys=True))
        return 0
    finally:
        db.close()


def cmd_run(args):
    db = open_db(args.db)
    try:
        phase = open_phase(db, utcnow())
        if phase is None:
            print("no open experiment phase; run start-phase first",
                  file=sys.stderr)
            return 2
        cfg = validate_config(json.loads(phase["config_json"]))
        if args.live:
            if args.confirm_budget is None or _dec(
                    args.confirm_budget) != _dec(phase["budget_usd"]):
                print("--live requires --confirm-budget equal to the "
                      f"phase budget ({phase['budget_usd']})",
                      file=sys.stderr)
                return 2
        lock_path = os.path.expanduser(args.db) + ".experiment.lock"
        lock_file = open(lock_path, "w")
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print(f"another run holds {lock_path}", file=sys.stderr)
            return 2
        stop = []

        def _stop(*_a):
            stop.append(True)

        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGTERM, _stop)
        mode = "LIVE" if args.live else "dry-run"
        print(f"run: phase {phase['phase_id']} ({mode}, "
              f"budget {phase['budget_usd']})")
        try:
            while not stop:
                try:
                    run_cycle(db, os.path.expanduser(args.db),
                              os.path.expanduser(args.env_file), phase,
                              cfg, live=args.live)
                except Exception as exc:
                    # one bad cycle must not kill the loop; never log
                    # exception text (may carry secrets)
                    try:
                        emit(db, utcnow(), phase["phase_id"], None,
                             None, "error",
                             {"error": type(exc).__name__})
                    except Exception:
                        pass
                if args.once:
                    break
                time.sleep(args.interval)
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)
            lock_file.close()
        return 0
    finally:
        db.close()


def cmd_status(args):
    db = open_db(args.db)
    try:
        now = utcnow()
        phase = open_phase(db, now)
        if phase is None:
            print("no open experiment phase")
            return 0
        cfg = validate_config(json.loads(phase["config_json"]))
        state = build_state(db, phase, now, cfg, apply_endings=False,
                            say=lambda *_a: None)
        print(f"phase {phase['phase_id']}: {_iso(now)} -> "
              f"{phase['ends_at']} budget ${state['budget']} "
              f"committed ${state['committed'].quantize(Decimal('0.01'))} "
              f"remaining ${state['remaining'].quantize(Decimal('0.01'))}")
        for r in state["rentals"]:
            print(f"  order {r['order_id']} server {r['server_id']} "
                  f"[{r['tier']}] age {str(r['age']).split('.')[0]} "
                  f"t0 {_iso(r['t0'])} obs_age "
                  f"{str(r['obs_age']).split('.')[0] if r['obs_age'] else '-'} "
                  f"its={r['its_total']} shares "
                  f"{sum(a for _, a in r['share_arrivals'])} "
                  f"last_share {_iso(r['last_share_at'])}")
        for pending in state["pending_rerents"]:
            print(f"  pending rerent: server {pending['server_id']} "
                  f"due {_iso(pending['due_at'])}")
        return 0
    finally:
        db.close()


def cmd_report(args):
    db = open_db(args.db)
    try:
        if args.phase is not None:
            phase = db.execute(
                "SELECT * FROM experiment_phases WHERE phase_id=?",
                (args.phase,)).fetchone()
        else:
            phase = db.execute(
                "SELECT * FROM experiment_phases ORDER BY phase_id DESC "
                "LIMIT 1").fetchone()
        if phase is None:
            print("no experiment phase")
            return 2
        cfg = validate_config(json.loads(phase["config_json"]))
        _report(db, phase, cfg)
        return 0
    finally:
        db.close()


def _report(db, phase, cfg):
    now = utcnow()
    phase_id = phase["phase_id"]
    listings = server_listings(db)
    xy = {}
    for row in db.execute(
            "SELECT * FROM experiment_rentals WHERE phase_id=? "
            "ORDER BY order_id", (phase_id,)):
        order_id = row["order_id"]
        good = _observations(db, order_id)
        arrivals = share_arrivals(good)
        shares = sum(a for _, a in arrivals)
        created, ended = _dt(row["created_at"]), _dt(row["ended_at"])
        span_end = ended or now
        hours = max(0.0, (span_end - created).total_seconds() / 3600.0)
        cost = _dec(row["hourly_usd"]) * Decimal(str(hours))
        if ended and (ended - created) > _minutes(cfg["free_window_min"]):
            cost += _dec(row["creation_fee_usd"])
        if row["ended_at"] is None:
            cost = _dec(row["quoted_cost_usd"])
        obs_hours = ((good[-1][0] - good[0][0]).total_seconds() / 3600.0
                     if good else 0.0)
        cpu_its = [s["its"]["cpu"] for _, s in good
                   if (s.get("its") or {}).get("cpu") is not None]
        gpu_its = [s["its"]["gpu"] for _, s in good
                   if (s.get("its") or {}).get("gpu") is not None]
        restarts = _restart_events(db, phase_id, order_id)
        segments = trees.tree_segments(db, order_id)
        seg_shares = []
        for seg in segments:
            start, end = _dt(seg["start_ts"]), _dt(seg["end_ts"])
            seg_shares.append(sum(
                a for ts, a in arrivals
                if ts >= start and (end is None or ts < end)))
        tree_changed = 0
        share_after = 0
        for ts in restarts:
            # a different tree id within 3 min of the restart
            before_seg = next(
                (s for s in segments
                 if _dt(s["start_ts"]) <= ts and
                 (s["end_ts"] is None or _dt(s["end_ts"]) > ts)), None)
            if any(s["tree_id"] != (before_seg or {}).get("tree_id")
                   and ts < _dt(s["start_ts"]) <= ts + _minutes(3)
                   for s in segments):
                tree_changed += 1
            if any(t <= ts + _minutes(5) and t >= ts
                   for t, _ in arrivals):
                share_after += 1
        listing = listings.get(row["server_id"]) or {}
        cpu = (listing.get("specs") or {}).get("cpu") or "-"
        gpu = market.offered_gpu(listing) or "-"
        print(f"order {order_id} server {row['server_id']} "
              f"[{row['tier']}] cpu={cpu} gpu={gpu} x={row['x_min']} "
              f"y={row['y_min']} observed={obs_hours:.2f}h "
              f"cost=${cost.quantize(Decimal('0.01'))} shares={shares} "
              f"sh/USD="
              f"{(Decimal(shares) / cost).quantize(Decimal('0.01')) if cost else '-'} "
              f"its(cpu med={_median(cpu_its)} gpu med={_median(gpu_its)}) "
              f"restarts={len(restarts)} tree_changed_3m={tree_changed} "
              f"share_5m={share_after}")
        for seg, seg_count in zip(segments, seg_shares):
            print(f"    tree {seg['tree_id']} epoch {seg['epoch']} "
                  f"{seg['start_ts']} -> {seg['end_ts'] or 'open'} "
                  f"shares={seg_count}")
        xy.setdefault(("x", row["x_min"]), []).append((shares, cost))
        xy.setdefault(("y", row["y_min"]), []).append((shares, cost))
    for (axis, value), rows in sorted(xy.items()):
        total_shares = sum(s for s, _ in rows)
        total_cost = sum(c for _, c in rows)
        per_usd = (Decimal(total_shares) / total_cost).quantize(
            Decimal("0.01")) if total_cost else "-"
        print(f"summary {axis}={value}: rentals={len(rows)} "
              f"shares={total_shares} cost=${total_cost.quantize(Decimal('0.01'))} "
              f"sh/USD={per_usd}")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Experiment-phase auto-rental runner")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("start-phase", help="open a new phase")
    sp.add_argument("--budget-usd", required=True)
    sp.add_argument("--phase-hours", type=float, default=48)
    sp.add_argument("--reserve-usd", default="0.50")
    sp.add_argument("--max-rentals", type=int)
    sp.add_argument("--hours", type=int)
    sp.add_argument("--max-price-hr")
    sp.add_argument("--x-min")
    sp.add_argument("--y-min")
    sp.add_argument("--startup-min", type=int)
    sp.add_argument("--startup-restart-every-min", type=int)
    sp.add_argument("--fault-check-min", type=int)
    sp.add_argument("--free-window-min", type=int)
    sp.add_argument("--dry-after-min", type=int)
    sp.add_argument("--restart-cooldown-min", type=int)
    sp.add_argument("--unreachable-min", type=int)
    sp.add_argument("--rerent-grace-min", type=int)
    sp.add_argument("--stale-obs-sec", type=int)
    sp.add_argument("--stale-market-min", type=int)
    _add_db_arg(sp)
    sp.set_defaults(func=cmd_start_phase)

    rp = sub.add_parser("run", help="run the decision loop")
    rp.add_argument("--live", action="store_true")
    rp.add_argument("--confirm-budget")
    rp.add_argument("--interval", type=float, default=60)
    rp.add_argument("--once", action="store_true")
    rp.add_argument("--env-file", default=DEFAULT_ENV)
    _add_db_arg(rp)
    rp.set_defaults(func=cmd_run)

    st = sub.add_parser("status", help="phase + rental state")
    _add_db_arg(st)
    st.set_defaults(func=cmd_status)

    rep = sub.add_parser("report", help="per-rental report")
    rep.add_argument("--phase", type=int)
    _add_db_arg(rep)
    rep.set_defaults(func=cmd_report)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
