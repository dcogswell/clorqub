"""Counterfactual replay: run the CURRENT decide()/blacklist_checks()
logic over a rewound copy of the live DB, applying each action's DB
effects the way execute() would -- minus the network calls.

What is real and what is simulated:
- Real world data streams back in on its recorded schedule: rig
  observations, marketplace snapshots, rent_failed/cancel_failed
  events, rig restarts, operator burst requests, blacklist rows.
- Real rentals (experiment_rentals/fleet_rentals rows) appear and end
  at their real timestamps, so rigs the sim keeps get their real
  telemetry. Real decision artifacts (rent/cancel/burst/judge events)
  are NOT replayed -- the sim makes its own.
- Simulated rents materialize after a provision delay and then stream
  telemetry cloned from that server's nearest real rental (same
  server_id, closest created_at). With no donor the rig streams
  "alive but no shares" status -- a pessimistic tree draw.
- Simulated cancels always succeed; wallet/quote/create checks that
  need the Clore API are approximated (a rent fails at materialize
  time only if the listing vanished from the snapshot).

Usage:
    .venv/bin/python scripts/replay_sim.py [--hours 24] [--step 15]
        [--db ~/.local/share/clorqub/clorqub.sqlite3] [--keep /tmp/x]
"""
import argparse
import heapq
import json
import os
import random
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import blacklist as blacklist_mod  # noqa: E402
import experiment as exp  # noqa: E402
import fleet_store  # noqa: E402
import market  # noqa: E402
import rental_policy  # noqa: E402

# events that are facts about the world, not decisions of the old
# logic: telemetry restarts, API failures, manual requests, burst
# metadata (params of bursts whose rows really exist)
WORLD_EVENT_KINDS = {
    "restart", "restart_failed", "rent_failed", "cancel_failed",
    "burst_requested", "error", "burst_started",
}
SIM_PROVISION_S = 120  # materialize a sim rent this long after decision
ENDED_STATES = exp.ENDED_STATES


def _iso(v):
    return v.isoformat() if isinstance(v, datetime) else v


def backup_db(src_path, dst_path):
    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    dst = sqlite3.connect(dst_path)
    with dst:
        src.backup(dst)
    src.close()
    return dst


# ------------------------------------------------------------- rewind

def stage_rewind(db, t0):
    """Move every post-t0 fact into pending tables; schedule the
    re-application of endings/judgements/removals that hadn't happened
    yet at t0. Returns counts staged."""
    t0s = _iso(t0)
    staged = {}
    db.executescript("""
    CREATE TABLE _p_obs(order_id INTEGER, captured_at TEXT,
                        status_json TEXT, error_kind TEXT);
    CREATE TABLE _p_events(ts TEXT, phase_id INTEGER, order_id INTEGER,
                           server_id INTEGER, kind TEXT,
                           detail_json TEXT, seq INTEGER);
    CREATE TABLE _p_snaps(id INTEGER, captured_at TEXT, source TEXT,
                          server_count INTEGER, raw_json TEXT);
    CREATE TABLE _p_rentals(order_id INTEGER, phase_id INTEGER,
        server_id INTEGER, tier TEXT, reason TEXT, x_min INTEGER,
        y_min INTEGER, hourly_usd TEXT, creation_fee_usd TEXT,
        quoted_cost_usd TEXT, created_at TEXT, role TEXT,
        burst_id INTEGER);
    CREATE TABLE _p_rental_ends(order_id INTEGER, ended_at TEXT,
                                end_reason TEXT);
    CREATE TABLE _p_fleet(order_id INTEGER, server_id INTEGER,
        alias TEXT, pool TEXT, deadline_at TEXT, max_cost_usd TEXT,
        quoted_cost_usd TEXT, ssh_key_path TEXT, auto_renew INTEGER,
        created_at TEXT, final_state TEXT, flip_at TEXT);
    CREATE TABLE _p_bursts(burst_id INTEGER, phase_id INTEGER,
        started_at TEXT, trigger TEXT, trigger_order_id INTEGER,
        n_target INTEGER, judged_at TEXT);
    CREATE TABLE _p_blacklist(id INTEGER, server_id INTEGER,
        reason TEXT, source TEXT, detail_json TEXT, added_at TEXT,
        expires_at TEXT, removed_at TEXT);
    CREATE TABLE _p_attempts(attempt_id TEXT, server_id INTEGER,
        alias TEXT, created_at TEXT, updated_at TEXT, state TEXT,
        order_id INTEGER);
    """)

    # observations
    db.execute(
        "INSERT INTO _p_obs SELECT order_id, captured_at, status_json,"
        " error_kind FROM rig_observations WHERE captured_at>?"
        " ORDER BY captured_at, id", (t0s,))
    staged["obs"] = db.execute("SELECT COUNT(*) FROM _p_obs").fetchone()[0]
    db.execute("DELETE FROM rig_observations WHERE captured_at>?", (t0s,))

    # events: keep only world-truth kinds in the pending stream; real
    # decision artifacts (rent/cancel/burst/judge notes/ticks) are not
    # replayed at all
    marks = ",".join("?" * len(WORLD_EVENT_KINDS))
    db.execute(
        f"INSERT INTO _p_events SELECT ts, phase_id, order_id,"
        f" server_id, kind, detail_json, id FROM experiment_events"
        f" WHERE ts>? AND kind IN ({marks}) ORDER BY ts, id",
        (t0s, *sorted(WORLD_EVENT_KINDS)))
    staged["events"] = db.execute(
        "SELECT COUNT(*) FROM _p_events").fetchone()[0]
    db.execute("DELETE FROM experiment_events WHERE ts>?", (t0s,))

    # market snapshots
    db.execute(
        "INSERT INTO _p_snaps SELECT id, captured_at, source,"
        " server_count, raw_json FROM marketplace_snapshots"
        " WHERE captured_at>? ORDER BY id", (t0s,))
    staged["snaps"] = db.execute("SELECT COUNT(*) FROM _p_snaps").fetchone()[0]
    db.execute("DELETE FROM marketplace_snapshots WHERE captured_at>?",
               (t0s,))

    # experiment rentals created after t0 are old-logic decisions --
    # the sim makes its own rents, so they are dropped outright. For
    # rows open at t0, only externally-forced endings re-apply on
    # schedule (the order really vanished server-side); endings the
    # old logic chose (dry/fault/cancelled/...) stay cleared and the
    # sim re-judges them. term_end needs no flip: build_state expires
    # the row at its fleet deadline regardless.
    db.execute(
        "INSERT INTO _p_rental_ends SELECT order_id, ended_at,"
        " end_reason FROM experiment_rentals WHERE ended_at>?"
        " AND end_reason IN ('missing','provision_failed')", (t0s,))
    staged["rental_ends"] = db.execute(
        "SELECT COUNT(*) FROM _p_rental_ends").fetchone()[0]
    db.execute(
        "UPDATE experiment_rentals SET ended_at=NULL, end_reason=NULL"
        " WHERE ended_at>?", (t0s,))
    db.execute("DELETE FROM experiment_rentals WHERE created_at>?", (t0s,))

    # fleet rows created after t0 belong to old-logic rentals: dropped
    # likewise. For rows live at t0, 'missing'/'provision_failed' are
    # external order losses and flip back on schedule; 'cancelled' was
    # a decision, so those stay live for the sim to manage.
    db.execute("DELETE FROM fleet_rentals WHERE created_at>?", (t0s,))
    for row in db.execute(
            "SELECT f.order_id, f.provision_state, f.updated_at,"
            " e.ended_at FROM fleet_rentals f LEFT JOIN"
            " experiment_rentals e ON e.order_id=f.order_id"
            " WHERE f.provision_state IN ('cancelled','missing',"
            "'provision_failed')"):
        flip = row["ended_at"] or row["updated_at"]
        external = row["provision_state"] != "cancelled"
        if flip and flip > t0s and external:
            db.execute(
                "INSERT INTO _p_fleet(order_id, final_state, flip_at)"
                " VALUES (?,?,?)",
                (row["order_id"], row["provision_state"], flip))
            staged["fleet_flips"] = staged.get("fleet_flips", 0) + 1
        if flip is None or flip > t0s:
            db.execute(
                "UPDATE fleet_rentals SET provision_state='ready'"
                " WHERE order_id=?", (row["order_id"],))

    # bursts started after t0 are decisions: dropped. A burst still
    # open at t0 is re-judged by the sim, not by the real verdict.
    db.execute("DELETE FROM experiment_bursts WHERE started_at>?", (t0s,))
    db.execute(
        "UPDATE experiment_bursts SET judged_at=NULL WHERE judged_at>?",
        (t0s,))

    # blacklist adds after t0 come from old-logic evidence checks:
    # dropped; the sim's own blacklist_checks rediscover what applies.
    # Removals are operator actions: kept on schedule.
    db.execute("DELETE FROM blacklist WHERE added_at>?", (t0s,))
    db.execute(
        "INSERT INTO _p_blacklist(id, removed_at) SELECT id,"
        " removed_at FROM blacklist WHERE removed_at>?", (t0s,))
    db.execute(
        "UPDATE blacklist SET removed_at=NULL WHERE removed_at>?", (t0s,))

    # rental attempts after t0 are old-logic decisions: dropped.
    # Attempts already in flight revert to 'creating' until their real
    # resolution time (a world fact about the API, not a decision).
    db.execute("DELETE FROM rental_attempts WHERE created_at>?", (t0s,))
    for row in db.execute(
            "SELECT attempt_id, server_id, alias, created_at,"
            " updated_at, state, order_id FROM rental_attempts"
            " WHERE updated_at>? AND state NOT IN"
            " ('creating','unknown')", (t0s,)):
        db.execute(
            "UPDATE rental_attempts SET state='creating', order_id=NULL"
            " WHERE attempt_id=?", (row["attempt_id"],))
        # created_at NULL marks a resolution-only staging row: the
        # attempt already exists (reverted above), only its outcome is
        # pending
        db.execute(
            "INSERT INTO _p_attempts VALUES (?,?,?,NULL,?,?,?)",
            (row["attempt_id"], row["server_id"], row["alias"],
             row["updated_at"], row["state"], row["order_id"]))
        staged["attempts_res"] = staged.get("attempts_res", 0) + 1

    db.commit()
    for t in ("_p_obs", "_p_events", "_p_snaps", "_p_rentals",
              "_p_rental_ends", "_p_fleet", "_p_bursts", "_p_blacklist",
              "_p_attempts"):
        col = {"_p_obs": "captured_at", "_p_events": "ts",
               "_p_snaps": "captured_at", "_p_rentals": "created_at",
               "_p_rental_ends": "ended_at", "_p_fleet": "created_at",
               "_p_bursts": "started_at", "_p_blacklist": "added_at",
               "_p_attempts": "created_at"}[t]
        db.execute(
            f"CREATE INDEX _pi_{t[3:]} ON {t}({col})")
    db.commit()
    return staged


def listings_at(db, t):
    """Marketplace reconstruction at time t. raw_json payloads are
    pruned to the newest snapshot, so listings are rebuilt from the
    normalized history: server_specs/server_prices valid windows plus
    servers.first_seen/last_seen membership. Volatile fields (rented,
    reliability, partial_gpu_rental, allowed_coins) fall back to the
    server's latest_json -- they can't be time-travelled."""
    ts = _iso(t)
    out = {}
    for r in db.execute(
            "SELECT s.server_id, s.latest_json, sp.specs_json,"
            " pr.price_json FROM servers s"
            " LEFT JOIN server_specs sp ON sp.server_id=s.server_id"
            " AND sp.observed_at<=?"
            " AND (sp.valid_to IS NULL OR sp.valid_to>?)"
            " LEFT JOIN server_prices pr ON pr.server_id=s.server_id"
            " AND pr.observed_at<=?"
            " AND (pr.valid_to IS NULL OR pr.valid_to>?)"
            " WHERE s.first_seen<=? AND s.last_seen>=?",
            (ts, ts, ts, ts, ts, ts)):
        try:
            doc = json.loads(r["latest_json"])
        except (ValueError, TypeError):
            continue
        if r["specs_json"]:
            try:
                sd = json.loads(r["specs_json"])
                doc["specs"] = sd.get("specs") or {}
                doc["gpu_array"] = sd.get("gpu_array") or []
                doc["cuda_version"] = sd.get("cuda_version")
            except ValueError:
                pass
        if r["price_json"]:
            try:
                doc["price"] = json.loads(r["price_json"])
            except ValueError:
                pass
        doc["id"] = r["server_id"]
        out[r["server_id"]] = doc
    snap = db.execute(
        "SELECT MAX(captured_at) FROM marketplace_snapshots"
    ).fetchone()[0]
    return out, exp._dt(snap) if snap else None


def apply_due(db, t):
    """Apply every pending world row with timestamp <= t."""
    ts = _iso(t)
    n = 0
    cur = db.execute(
        "INSERT INTO rig_observations(order_id, captured_at,"
        " status_json, error_kind) SELECT order_id, captured_at,"
        " status_json, error_kind FROM _p_obs WHERE captured_at<=?"
        " ORDER BY captured_at, rowid", (ts,))
    n += cur.rowcount
    db.execute("DELETE FROM _p_obs WHERE captured_at<=?", (ts,))

    cur = db.execute(
        "INSERT INTO experiment_events(ts, phase_id, order_id,"
        " server_id, kind, detail_json) SELECT ts, phase_id, order_id,"
        " server_id, kind, detail_json FROM _p_events WHERE ts<=?"
        " ORDER BY ts, seq", (ts,))
    n += cur.rowcount
    db.execute("DELETE FROM _p_events WHERE ts<=?", (ts,))

    cur = db.execute(
        "INSERT INTO marketplace_snapshots(id, captured_at, source,"
        " server_count, raw_json) SELECT id, captured_at, source,"
        " server_count, raw_json FROM _p_snaps WHERE captured_at<=?"
        " ORDER BY id", (ts,))
    n += cur.rowcount
    db.execute("DELETE FROM _p_snaps WHERE captured_at<=?", (ts,))

    cur = db.execute(
        "UPDATE experiment_rentals SET ended_at=(SELECT ended_at FROM"
        " _p_rental_ends p WHERE p.order_id=experiment_rentals.order_id),"
        " end_reason=(SELECT end_reason FROM _p_rental_ends p WHERE"
        " p.order_id=experiment_rentals.order_id) WHERE ended_at IS NULL"
        " AND order_id IN (SELECT order_id FROM _p_rental_ends"
        " WHERE ended_at<=?)", (ts,))
    n += cur.rowcount
    db.execute("DELETE FROM _p_rental_ends WHERE ended_at<=?", (ts,))

    for row in db.execute(
            "SELECT rowid, * FROM _p_fleet WHERE created_at IS NOT NULL"
            " AND created_at<=?", (ts,)).fetchall():
        db.execute(
            "INSERT INTO fleet_rentals(order_id, server_id, alias,"
            " pool, deadline_at, max_cost_usd, quoted_cost_usd,"
            " ssh_key_path, auto_renew, created_at, updated_at,"
            " provision_state) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (row["order_id"], row["server_id"], row["alias"],
             row["pool"], row["deadline_at"], row["max_cost_usd"],
             row["quoted_cost_usd"], row["ssh_key_path"],
             row["auto_renew"], row["created_at"], row["created_at"],
             "pending"))
        if row["final_state"] in ENDED_STATES:
            # flip later, at the recorded end time
            db.execute(
                "UPDATE _p_fleet SET created_at=NULL WHERE rowid=?",
                (row["rowid"],))
        else:
            db.execute("DELETE FROM _p_fleet WHERE rowid=?",
                       (row["rowid"],))
        n += 1
    cur = db.execute(
        "UPDATE fleet_rentals SET provision_state=(SELECT final_state"
        " FROM _p_fleet p WHERE p.order_id=fleet_rentals.order_id"
        " AND p.flip_at<=?), updated_at=? WHERE order_id IN"
        " (SELECT order_id FROM _p_fleet WHERE final_state IS NOT NULL"
        " AND flip_at<=?)", (ts, ts, ts))
    n += cur.rowcount
    db.execute(
        "DELETE FROM _p_fleet WHERE final_state IS NOT NULL"
        " AND flip_at<=?", (ts,))

    cur = db.execute(
        "INSERT INTO blacklist(id, server_id, reason, source,"
        " detail_json, added_at, expires_at) SELECT id, server_id,"
        " reason, source, detail_json, added_at, expires_at FROM"
        " _p_blacklist WHERE added_at IS NOT NULL AND added_at<=?",
        (ts,))
    n += cur.rowcount
    db.execute(
        "UPDATE _p_blacklist SET added_at=NULL WHERE added_at<=?", (ts,))
    cur = db.execute(
        "UPDATE blacklist SET removed_at=(SELECT removed_at FROM"
        " _p_blacklist p WHERE p.id=blacklist.id) WHERE removed_at"
        " IS NULL AND id IN (SELECT id FROM _p_blacklist WHERE"
        " added_at IS NULL AND removed_at<=?)", (ts,))
    n += cur.rowcount
    db.execute(
        "DELETE FROM _p_blacklist WHERE added_at IS NULL"
        " AND removed_at<=?", (ts,))

    for row in db.execute(
            "SELECT rowid, * FROM _p_attempts WHERE created_at<=?",
            (ts,)).fetchall():
        db.execute(
            "INSERT INTO rental_attempts(attempt_id, server_id, alias,"
            " before_ids_json, quote_json, state, order_id,"
            " created_at, updated_at) VALUES (?,?,?,'[]','{}',"
            " 'creating', NULL, ?, ?)",
            (row["attempt_id"], row["server_id"], row["alias"],
             row["created_at"], row["created_at"]))
        db.execute(
            "UPDATE _p_attempts SET created_at=NULL WHERE rowid=?",
            (row["rowid"],))
        n += 1
    cur = db.execute(
        "UPDATE rental_attempts SET state=(SELECT state FROM"
        " _p_attempts p WHERE p.attempt_id=rental_attempts.attempt_id),"
        " order_id=(SELECT order_id FROM _p_attempts p WHERE"
        " p.attempt_id=rental_attempts.attempt_id), updated_at=?"
        " WHERE attempt_id IN (SELECT attempt_id FROM _p_attempts"
        " WHERE created_at IS NULL AND updated_at<=?)", (ts, ts))
    n += cur.rowcount
    db.execute(
        "DELETE FROM _p_attempts WHERE created_at IS NULL"
        " AND updated_at<=?", (ts,))
    db.commit()
    return n


# ---------------------------------------------------- sim side-effects

def sim_blacklist(db, phase_id, action, now, log):
    bid = blacklist_mod.add(db, action["server_id"], action["reason"],
                            "auto", detail=action.get("detail"),
                            now=now)
    if bid is not None:
        exp.emit(db, now, phase_id, action.get("order_id"),
                 action["server_id"], "blacklisted",
                 {"reason": action["reason"],
                  "detail": action.get("detail") or {},
                  "ttl_days": blacklist_mod.AUTO_TTL_DAYS},
                 say=lambda *_: None)
        log.append((now, "blacklist", action["server_id"],
                    action["reason"], None))


def sim_cancel(db, phase_id, action, now, cfg, log):
    order_id = action["order_id"]
    failed_at = exp._last_cancel_failed(db, phase_id, order_id)
    if failed_at is not None \
            and now - failed_at < exp._minutes(exp.CANCEL_RETRY_MIN):
        row = db.execute(
            "SELECT created_at FROM experiment_rentals WHERE order_id=?",
            (order_id,)).fetchone()
        in_free = row is not None and now - exp._dt(
            row["created_at"]) < exp._minutes(cfg["free_window_min"])
        if not in_free:
            exp._emit_blocked(db, now, phase_id, order_id,
                              action["server_id"],
                              "cancel_retry_backoff", say=lambda *_: None)
            return False
    with db:
        db.execute(
            "UPDATE experiment_rentals SET ended_at=?, end_reason=?"
            " WHERE order_id=?", (_iso(now), action["reason"], order_id))
        db.execute(
            "UPDATE fleet_rentals SET provision_state='cancelled',"
            " updated_at=? WHERE order_id=?", (_iso(now), order_id))
    exp.emit(db, now, phase_id, order_id, action["server_id"], "cancel",
             {"reason": action["reason"], "its": action.get("its"),
              "shares": action.get("shares"),
              "age_min": action.get("age_min")}, say=lambda *_: None)
    if action["reason"].startswith("fault:") or \
            action["reason"] == "unreachable":
        sim_blacklist(db, phase_id, {
            **action, "detail": {"order_id": order_id,
                                 "cancel_confirmed": True}}, now, log)
    if action.get("rerent_at"):
        exp.emit(db, now, phase_id, order_id, action["server_id"],
                 "rerent_scheduled",
                 {"server_id": action["server_id"],
                  "tier": action.get("tier"),
                  "due_at": action["rerent_at"]}, say=lambda *_: None)
    if action.get("retry_burst"):
        exp.emit(db, now, phase_id, order_id, action["server_id"],
                 "burst_scheduled", action["retry_burst"],
                 say=lambda *_: None)
    return True


def sim_burst(db, phase_id, action, now):
    with db:
        cur = db.execute(
            "INSERT INTO experiment_bursts(phase_id, started_at,"
            " trigger, trigger_order_id, n_target) VALUES (?,?,?,?,?)",
            (phase_id, _iso(now), action["trigger"],
             action.get("trigger_order_id"), action["n"]))
    exp.emit(db, now, phase_id, action.get("trigger_order_id"), None,
             "burst_started",
             {"burst_id": cur.lastrowid, "trigger": action["trigger"],
              "n": action["n"], "y_min": action.get("y_min"),
              "server_id": action.get("server_id")}, say=lambda *_: None)
    return cur.lastrowid


def sim_judge(db, phase_id, action, now, cfg, log):
    for oid in [action.get("promote")] + [
            e["order_id"] for e in action.get("promote_extra") or []]:
        if oid is None:
            continue
        db.execute(
            "UPDATE experiment_rentals SET role='slot' WHERE order_id=?"
            " AND role='probe'", (oid,))
    db.execute(
        "UPDATE experiment_bursts SET judged_at=? WHERE burst_id=?",
        (_iso(now), action["burst_id"]))
    db.commit()

    def _ctx(c):
        return {"order_id": c["order_id"], "server_id": c["server_id"],
                "tier": c.get("tier"), "reason": c["reason"],
                "its": c.get("its"), "shares": c.get("shares"),
                "age_min": c.get("age_min"), "rerent_at": None}

    for c in action["cancel"]:
        sim_cancel(db, phase_id, _ctx(c), now, cfg, log)
    if action.get("cancel_original"):
        sim_cancel(db, phase_id, _ctx(action["cancel_original"]),
                   now, cfg, log)
    if action.get("retry"):
        exp.emit(db, now, phase_id, action["retry"]["for_order"],
                 action["retry"].get("server_id"), "burst_scheduled",
                 action["retry"], say=lambda *_: None)
    exp.emit(db, now, phase_id, None, None, "burst_judged",
             {"burst_id": action["burst_id"],
              "verdict": action["verdict"],
              "finders": action["finders"],
              "promote": action.get("promote"),
              "promoted_extra": action.get("promote_extra") or [],
              "cancelled": [c["order_id"] for c in action["cancel"]],
              "cancel_original":
                  (action.get("cancel_original") or {}).get("reason"),
              "retry_due_at": (action.get("retry") or {}).get("due_at"),
              "stats": action.get("stats")}, say=lambda *_: None)


_HW_CACHE = {}
_ORDER_SID_CACHE = {}


def server_hw(src, key, by_order=False):
    """(cpu, gpu) class of a server_id or a fleet order_id, from the
    pristine db's latest listing. None when unknown."""
    if key in _HW_CACHE:
        return _HW_CACHE[key]
    sid = key
    if by_order:
        sid = _ORDER_SID_CACHE.get(key)
        if sid is None:
            row = src.execute(
                "SELECT server_id FROM fleet_rentals WHERE order_id=?",
                (key,)).fetchone()
            sid = row["server_id"] if row else None
            _ORDER_SID_CACHE[key] = sid
    hw = None
    if sid is not None:
        row = src.execute(
            "SELECT latest_json FROM servers WHERE server_id=?",
            (sid,)).fetchone()
        if row and row["latest_json"]:
            try:
                doc = json.loads(row["latest_json"])
                hw = (market.cpu_profile(
                        (doc.get("specs") or {}).get("cpu")),
                      market.offered_gpu(doc))
            except ValueError:
                pass
    _HW_CACHE[key] = hw
    return hw


_SHARE_POOL = []


def _share_pool(src):
    """Empirical share-arrival timelines: (rental_median_its,
    [seconds from t0 to each arrival]) for real rentals with enough
    coverage to show whether they produce."""
    if _SHARE_POOL:
        return _SHARE_POOL
    for f in src.execute("SELECT order_id FROM fleet_rentals"):
        rows = src.execute(
            "SELECT captured_at, status_json FROM rig_observations"
            " WHERE order_id=? AND status_json IS NOT NULL ORDER BY id",
            (f["order_id"],)).fetchall()
        good = [(exp._dt(r["captured_at"]), json.loads(r["status_json"]))
                for r in rows]
        if len(good) < 20:
            continue
        t0 = good[0][0]
        if (good[-1][0] - t0).total_seconds() < 1200:
            continue
        arrivals = [(a - t0).total_seconds()
                    for a, _ in exp.share_arrivals(good)]
        its = exp._median([exp._its_total(s) for _, s in good])
        _SHARE_POOL.append((its or 0.0, arrivals))
    return _SHARE_POOL


def _patch_status_hw(raw, listing):
    """Rewrite a donor status's hardware block to the listing's own
    specs -- the sim rig really has the listed cpu/gpu; the donor only
    lends it/s dynamics and share cadence."""
    try:
        status = json.loads(raw)
    except (ValueError, TypeError):
        return raw
    specs = (listing or {}).get("specs") or {}
    hw = status.setdefault("hardware", {})
    if specs.get("cpu"):
        hw["cpu_model"] = specs["cpu"]
    threads = rental_policy.offered_threads(specs)
    if threads:
        hw["nproc"] = threads
    n_gpus = len((listing or {}).get("gpu_array") or [])
    if n_gpus:
        hw["gpus"] = [{"name": g} for g in
                      (listing.get("gpu_array") or [])]
    return json.dumps(status, sort_keys=True)


def clone_or_fabricate_obs(db, src, order_id, server_id, at, deadline,
                           hourly_stats, listing):
    """Stream telemetry for a simulated rig. Best donor: the same
    server's nearest real rental. Next: the same (cpu, gpu) hardware
    class -- another rig's real obs give faithful it/s AND share
    cadence. When the donor stream runs out before the deadline the
    last good status repeats (alive, quiet). With no class donor at
    all the rig streams an alive-but-shareless placeholder (a
    pessimistic tree draw)."""
    staged = 0

    def _donor_rows(where, params):
        return [dict(r) for r in src.execute(
            "SELECT f.order_id, MIN(o.captured_at) AS first_ts,"
            " COUNT(*) n FROM fleet_rentals f JOIN rig_observations o"
            " ON o.order_id=f.order_id WHERE " + where +
            " GROUP BY f.order_id", params)]

    donors = _donor_rows("f.server_id=?", (server_id,))
    kind = "clone"
    if not donors:
        # hardware-class donor: same cpu/gpu model as this listing
        mine = server_hw(src, server_id)
        if mine is not None:
            cand = _donor_rows("f.server_id!=?", (server_id,))
            same = [d for d in cand
                    if server_hw(src, d["order_id"], by_order=True)
                    == mine]
            donors = same
            kind = "clone_hw"
    if donors:
        donors.sort(key=lambda d: (d["n"] < 60,
                                   abs((exp._dt(d["first_ts"]) - at)
                                       .total_seconds())))
        donor = donors[0]["order_id"]
        rows = src.execute(
            "SELECT captured_at, status_json, error_kind FROM"
            " rig_observations WHERE order_id=? ORDER BY id",
            (donor,)).fetchall()
        base = exp._dt(rows[0]["captured_at"])
        last_good = None
        last_ts = None
        for r in rows:
            shifted = at + (exp._dt(r["captured_at"]) - base)
            if shifted > deadline:
                break
            db.execute(
                "INSERT INTO _p_obs VALUES (?,?,?,?)",
                (order_id, _iso(shifted),
                 _patch_status_hw(r["status_json"], listing)
                 if r["status_json"] is not None else None,
                 r["error_kind"]))
            staged += 1
            last_ts = shifted
            if r["status_json"] is not None:
                last_good = _patch_status_hw(r["status_json"], listing)
        # donor ran out: hold the last good status (alive, no new
        # shares) so the rig ages naturally instead of going silent
        if last_good is not None and last_ts is not None:
            t = last_ts + timedelta(seconds=60)
            while t <= deadline:
                db.execute(
                    "INSERT INTO _p_obs VALUES (?,?,?,NULL)",
                    (order_id, _iso(t), last_good))
                t += timedelta(seconds=60)
                staged += 1
        return kind, donor, staged
    cpu_p, gpu_p = hourly_stats
    total = (cpu_p or 0) + (gpu_p or 0) or 50.0
    if not cpu_p:
        cpu_p = round(total * 0.3, 2)
    if not gpu_p:
        gpu_p = round(total * 0.7, 2)
    # share timeline: a seeded draw from the real fleet's arrival
    # history, gap-scaled by donor/sim it/s -- so an unheard-of rig
    # gets an average real production profile rather than a certain
    # barren tree
    arrivals = []
    pool = _share_pool(src)
    if pool:
        donor_its, rel = random.Random(order_id).choice(pool)
        scale = (donor_its or total) / total
        arrivals = [at + timedelta(seconds=s * scale) for s in rel]
    specs = (listing or {}).get("specs") or {}
    n_gpus = max(1, len((listing or {}).get("gpu_array") or []))
    status = {
        "its": {"cpu": cpu_p, "gpu": gpu_p, "total": cpu_p + gpu_p},
        "shares": {"submitted": 0},
        "hardware": {
            "cpu_model": specs.get("cpu") or "unknown",
            "nproc": rental_policy.offered_threads(specs) or 32,
            "gpus": [{"name": specs.get("gpu") or "gpu"}] * n_gpus},
        "pool": exp.POOL,
        "package": {"version": "sim"},
        "trainer": {"variant": "sim"},
    }
    t = at
    n_arr = 0
    while t <= deadline:
        while n_arr < len(arrivals) and arrivals[n_arr] <= t:
            n_arr += 1
        status["shares"]["submitted"] = n_arr
        db.execute(
            "INSERT INTO _p_obs VALUES (?,?,?,NULL)",
            (order_id, _iso(t), json.dumps(status, sort_keys=True)))
        t += timedelta(seconds=60)
        staged += 1
    return "fabricated", None, staged


# ------------------------------------------------------------- driver

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.expanduser(
        "~/.local/share/clorqub/clorqub.sqlite3"))
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--step", type=float, default=15,
                    help="tick seconds (runner uses 15)")
    ap.add_argument("--keep", help="keep the replay db copy at PATH")
    args = ap.parse_args()

    tmp = args.keep or "/tmp/replay_sim.sqlite3"
    if os.path.exists(tmp):
        os.remove(tmp)
    for suffix in ("-wal", "-shm"):
        if os.path.exists(tmp + suffix):
            os.remove(tmp + suffix)
    db = backup_db(args.db, tmp)
    db.row_factory = sqlite3.Row
    src = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row

    now = datetime.now(timezone.utc).replace(microsecond=0)
    phase = db.execute(
        "SELECT * FROM experiment_phases WHERE closed_at IS NULL"
        " ORDER BY phase_id DESC LIMIT 1").fetchone()
    if phase is None:
        phase = db.execute(
            "SELECT * FROM experiment_phases ORDER BY phase_id DESC"
            " LIMIT 1").fetchone()
    cfg = exp.validate_config(json.loads(phase["config_json"]))
    phase = dict(phase)
    t0 = max(now - timedelta(hours=args.hours),
             exp._dt(phase["started_at"]))
    end = min(now, exp._dt(phase["ends_at"]))
    phase_id = phase["phase_id"]

    staged = stage_rewind(db, t0)
    print(f"# replay window {_iso(t0)} .. {_iso(end)} "
          f"phase={phase_id} budget={phase['budget_usd']}")
    print("# staged:", json.dumps(staged, sort_keys=True))
    print("# cfg:", json.dumps({k: cfg[k] for k in sorted(cfg)
                                if not isinstance(cfg[k], (list,))},
                               sort_keys=True))

    # listings are rebuilt from spec/price history, which only changes
    # at snapshot ingest times -- memoize on the newest applied
    # snapshot's captured_at
    sim_now = [t0]
    snap_cache = {}

    def cached_latest(d):
        key = d.execute(
            "SELECT MAX(captured_at) FROM marketplace_snapshots"
        ).fetchone()[0]
        if snap_cache.get("key") != key:
            snap_cache.clear()
            score_cache.clear()
            snap_cache["key"] = key
            snap_cache["v"] = listings_at(d, sim_now[0])
        return snap_cache["v"]
    exp.latest_listings = cached_latest

    sl_cache = []
    real_sl = exp.server_listings

    def cached_sl(d):
        if not sl_cache:
            sl_cache.append(real_sl(d))
        return sl_cache[0]
    exp.server_listings = cached_sl

    # watermark memoization: inputs change only when rows are applied
    def _fleet_mark(d):
        r = d.execute(
            "SELECT COUNT(*), COALESCE(MAX(order_id),0) FROM"
            " fleet_rentals").fetchone()
        return r[0], r[1]

    obs_cache = {}
    real_obs = exp._observations

    def cached_obs(d, order_id):
        key = d.execute(
            "SELECT MAX(id) FROM rig_observations WHERE order_id=?",
            (order_id,)).fetchone()[0]
        ent = obs_cache.get(order_id)
        if ent is not None and ent[0] == key:
            return ent[1]
        v = real_obs(d, order_id)
        obs_cache[order_id] = (key, v)
        return v
    exp._observations = cached_obs

    score_cache = {}
    real_score = market.score_listing

    def cached_score(server, mode="on-demand", benchmarks=None):
        key = id(server)
        v = score_cache.get(key)
        if v is None:
            v = real_score(server, mode=mode, benchmarks=benchmarks)
            score_cache[key] = v
        return v
    market.score_listing = cached_score

    est_cache = {}
    real_est = exp.est_its

    def cached_est(stats, server_id, cpu, gpu):
        key = (server_id, cpu, gpu, id(stats))
        v = est_cache.get(key)
        if v is None:
            v = real_est(stats, server_id, cpu, gpu)
            if len(est_cache) < 200000:
                est_cache[key] = v
        return v
    exp.est_its = cached_est

    hs_cache = {}
    real_hs = exp.history_stats

    def cached_hs(d, known_listings, exclude_order_id=None):
        mark = (d.execute(
            "SELECT MAX(id) FROM rig_observations").fetchone()[0],
            _fleet_mark(d), exclude_order_id)
        v = hs_cache.get(mark)
        if v is None:
            v = real_hs(d, known_listings, exclude_order_id)
            hs_cache.clear()
            hs_cache[mark] = v
        return v
    exp.history_stats = cached_hs

    log = []          # (ts, type, target, reason, extra)
    notes_at = []     # (ts, [note lines])
    blocked_marks = {}  # frozenset(reasons) -> [start, last]
    pending_rents = []  # heap (due, seq, spec)
    sim_oid = [-10]     # negative synthetic order ids
    rented_flag = [False]

    def sim_rent(action, t, state):
        sid = action["server_id"]
        if rented_flag[0]:
            exp._emit_blocked(db, t, phase_id, None, sid,
                              "one_rent_per_cycle", say=lambda *_: None)
            log.append((t, "blocked", sid, "one_rent_per_cycle", None))
            return
        if sid in blacklist_mod.active(db, t):
            exp._emit_blocked(db, t, phase_id, None, sid, "blacklisted",
                              say=lambda *_: None)
            log.append((t, "blocked", sid, "blacklisted", None))
            return
        rented_flag[0] = True
        attempt_id = f"sim{abs(sim_oid[0])}-{sid}"
        with db:
            db.execute(
                "INSERT INTO rental_attempts(attempt_id, server_id,"
                " alias, before_ids_json, quote_json, state, order_id,"
                " created_at, updated_at) VALUES (?,?,?,'[]','{}',"
                " 'creating', NULL, ?, ?)",
                (attempt_id, sid, f"sim-{sid}", _iso(t), _iso(t)))
        spec = {"order_id": sim_oid[0], "server_id": sid,
                "attempt_id": attempt_id, "tier": action["tier"],
                "reason": action["reason"], "role": action.get("role",
                                                              "slot"),
                "hours": action.get("hours", cfg["hours"]),
                "burst_id": action.get("burst_id"), "decided_at": t}
        sim_oid[0] -= 1
        heapq.heappush(pending_rents,
                       (t + timedelta(seconds=SIM_PROVISION_S),
                        sim_oid[0], spec))

    # real rent outcomes: a server that actually failed near the sim
    # rent time was very likely unrentable then too; a run of real
    # quote failures with no successes marks a systemic outage any
    # attempt inside would have hit
    fail_by_sid = {}
    fail_times = []
    ok_times = []
    for r in src.execute(
            "SELECT ts, server_id, detail_json FROM experiment_events"
            " WHERE kind='rent_failed' AND ts>=?", (_iso(t0),)):
        d = json.loads(r["detail_json"])
        rec = (exp._dt(r["ts"]), d.get("stage") or "unknown")
        if r["server_id"] is not None:
            fail_by_sid.setdefault(r["server_id"], []).append(rec)
        if rec[1] == "quote":
            fail_times.append(rec[0])
    for r in src.execute(
            "SELECT ts FROM experiment_events WHERE kind='rent'"
            " AND ts>=?", (_iso(t0),)):
        ok_times.append(exp._dt(r["ts"]))
    fail_times.sort()
    ok_times.sort()

    import bisect

    def _near(times, m, window_s):
        i = bisect.bisect_left(times, m - timedelta(seconds=window_s))
        return any(times[j] <= m + timedelta(seconds=window_s)
                   for j in range(i, len(times)))

    def _rent_fails(sid, m):
        for ts, stage in fail_by_sid.get(sid, []):
            if abs((ts - m).total_seconds()) <= 20 * 60:
                return stage
        lo = m - timedelta(minutes=6)
        hi = m + timedelta(minutes=6)
        n_fail = sum(1 for t in fail_times if lo <= t <= hi)
        if n_fail >= 4 and not _near(ok_times, m, 10 * 60):
            return "quote"
        return None

    def materialize(spec, m):
        sid = spec["server_id"]
        fail_stage = _rent_fails(sid, m)
        if fail_stage is not None:
            db.execute(
                "UPDATE rental_attempts SET state='failed',"
                " updated_at=? WHERE attempt_id=?",
                (_iso(m), spec["attempt_id"]))
            exp.emit(db, m, phase_id, None, sid, "rent_failed",
                     {"stage": fail_stage,
                      "detail": "sim: mirrored real failure"},
                     say=lambda *_: None)
            if fail_stage != "quote":
                sim_blacklist(db, phase_id, {
                    "server_id": sid, "order_id": None,
                    "reason": "provision_failed",
                    "detail": {"sim": True}}, m, log)
            log.append((m, "rent_failed", sid,
                        f"mirrored real {fail_stage} failure",
                        spec["reason"]))
            db.commit()
            return
        listings = cached_latest(db)[0]
        listing = listings.get(sid)
        if listing is None:
            db.execute(
                "UPDATE rental_attempts SET state='no_order',"
                " updated_at=? WHERE attempt_id=?",
                (_iso(m), spec["attempt_id"]))
            exp.emit(db, m, phase_id, None, sid, "rent_failed",
                     {"stage": "quote", "detail": "sim: listing "
                      "gone at materialize"}, say=lambda *_: None)
            log.append((m, "rent_failed", sid,
                        "listing gone (sim)", spec["reason"]))
            db.commit()
            return
        row = market.score_listing(listing)
        hourly = exp._dec(row["hourly_cost_usd"] or "0")
        hours = spec["hours"]
        fee = exp._CREATION_FEE_USD
        quoted = hourly * hours + exp._QUOTE_PAD_USD
        deadline = m + timedelta(hours=hours)
        count = db.execute(
            "SELECT COUNT(*) FROM experiment_rentals WHERE phase_id=?",
            (phase_id,)).fetchone()[0]
        x_min = cfg["x_min"][count % len(cfg["x_min"])]
        y_min = cfg["y_min"][count % len(cfg["y_min"])]
        oid = spec["order_id"]
        with db:
            db.execute(
                "INSERT INTO fleet_rentals(order_id, server_id, alias,"
                " pool, deadline_at, max_cost_usd, quoted_cost_usd,"
                " ssh_key_path, ssh_host, ssh_port, provision_state,"
                " auto_renew, created_at, updated_at) VALUES (?,?,?,"
                " 'jetski', ?, NULL, ?, 'sim', NULL, NULL, 'ready', 0,"
                " ?, ?)",
                (oid, sid, f"sim-{sid}", _iso(deadline), str(quoted),
                 _iso(m), _iso(m)))
            db.execute(
                "INSERT INTO experiment_rentals(order_id, phase_id,"
                " server_id, tier, reason, x_min, y_min, hourly_usd,"
                " creation_fee_usd, quoted_cost_usd, created_at,"
                " role, burst_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (oid, phase_id, sid, spec["tier"], spec["reason"],
                 x_min, y_min, str(hourly), str(fee), str(quoted),
                 _iso(m), spec["role"], spec["burst_id"]))
            db.execute(
                "UPDATE rental_attempts SET state='matched',"
                " order_id=?, updated_at=? WHERE attempt_id=?",
                (oid, _iso(m), spec["attempt_id"]))
        stats = exp.history_stats(db, cached_sl(db))
        est, _x, _ce = exp.est_its(stats, sid, row["cpu"], row["gpu"])
        cpu_p, _, _ = exp._cpu_est(stats, row["cpu"])
        gpu_p, _, _ = exp._gpu_est(stats, row["gpu"])
        obs_src, donor, staged_n = clone_or_fabricate_obs(
            db, src, oid, sid, m, deadline, (cpu_p, gpu_p), listing)
        exp.emit(db, m, phase_id, oid, sid, "rent",
                 {"tier": spec["tier"], "reason": spec["reason"],
                  "role": spec["role"], "hours": hours,
                  "hourly_usd": str(hourly), "x_min": x_min,
                  "y_min": y_min, "quoted_cost_usd": str(quoted),
                  "sim": True, "obs_source": obs_src,
                  "donor": donor}, say=lambda *_: None)
        log.append((m, "rent", sid, spec["reason"],
                    {"order_id": oid, "role": spec["role"],
                     "hourly": str(hourly), "hours": hours,
                     "decided_at": _iso(spec["decided_at"]),
                     "obs": obs_src, "donor": donor,
                     "n_obs": staged_n, "est_its": est}))
        db.commit()

    step = timedelta(seconds=args.step)
    t = t0
    prev_blocks = frozenset()
    tick = 0
    while t <= end:
        tick += 1
        sim_now[0] = t
        apply_due(db, t)
        while pending_rents and pending_rents[0][0] <= t:
            _, _, spec = heapq.heappop(pending_rents)
            materialize(spec, min(spec["decided_at"]
                                  + timedelta(seconds=SIM_PROVISION_S),
                                  t))
        state = exp.build_state(db, phase, t, cfg, apply_endings=False,
                                say=lambda *_: None)
        checks = [{"type": "blacklist", "order_id": oid,
                   "server_id": sid, "reason": reason,
                   "detail": detail}
                  for oid, sid, reason, detail in
                  exp.blacklist_checks(db, state, t, cfg)]
        state["excluded"].update(a["server_id"] for a in checks)
        notes = []
        acts = checks + exp.decide(t, state, cfg, say=notes.append)
        if notes:
            notes_at.append((t, list(notes)))
        blocks = frozenset(a["reason"] for a in acts
                           if a["type"] == "blocked")
        if blocks != prev_blocks:
            if prev_blocks:
                blocked_marks.setdefault(prev_blocks, []).append(t)
            if blocks:
                blocked_marks.setdefault(blocks, []).append(t)
            prev_blocks = blocks

        rented_flag[0] = False
        for a in acts:
            kind = a["type"]
            if kind == "blocked":
                continue
            if kind == "blacklist":
                sim_blacklist(db, phase_id, a, t, log)
            elif kind in ("rerent_expired", "retry_expired",
                          "retry_cleared"):
                exp.emit(db, t, phase_id, a.get("for_order"),
                         a.get("server_id"), kind,
                         {"server_id": a.get("server_id"),
                          "for_order": a.get("for_order")},
                         say=lambda *_: None)
                log.append((t, kind, a.get("server_id")
                            or a.get("for_order"), None, None))
            elif kind == "burst":
                bid = sim_burst(db, phase_id, a, t)
                log.append((t, "burst", a.get("server_id"),
                            a["trigger"],
                            {"burst_id": bid,
                             "for_order": a.get("trigger_order_id"),
                             "n": a["n"]}))
            elif kind == "judge":
                sim_judge(db, phase_id, a, t, cfg, log)
                log.append((t, "judge", a["burst_id"], a["verdict"],
                            {"promote": a.get("promote"),
                             "cancel": [c["order_id"]
                                        for c in a["cancel"]],
                             "cancel_original":
                                 (a.get("cancel_original") or {})
                                 .get("order_id"),
                             "stats": a.get("stats")}))
            elif kind == "cancel":
                if sim_cancel(db, phase_id, a, t, cfg, log):
                    log.append((t, "cancel", a["order_id"],
                                a["reason"],
                                {"server_id": a["server_id"],
                                 "its": a.get("its"),
                                 "shares": a.get("shares"),
                                 "age_min": a.get("age_min")}))
            elif kind == "rent":
                sim_rent(a, t, state)
        t += step

    # --------------------------------------------------------------
    # report
    print("\n=== actions (sim timeline) ===")
    for ts, kind, target, reason, extra in log:
        line = f"{_iso(ts)} {kind:<14} {target}"
        if reason:
            line += f" {reason}"
        if extra:
            line += " " + json.dumps(extra, default=str, sort_keys=True)
        print(line)

    print("\n=== decision notes on action ticks ===")
    action_ts = {ts for ts, *_ in log}
    for ts, notes in notes_at:
        if ts in action_ts:
            for n in notes:
                print(n)

    print("\n=== blocked windows ===")
    for reasons, marks in blocked_marks.items():
        if reasons:
            print(f"  {sorted(reasons)}: first {marks[0]} "
                  f"last {marks[-1]}")

    print("\n=== real events (what actually happened, for diff) ===")
    for r in src.execute(
            "SELECT ts, order_id, server_id, kind, detail_json FROM"
            " experiment_events WHERE ts>=? AND kind IN ('rent',"
            " 'cancel', 'ended', 'burst_started', 'burst_judged',"
            " 'blacklisted', 'rent_failed', 'rerent_scheduled',"
            " 'burst_scheduled') ORDER BY ts", (_iso(t0),)):
        print(f"{r['ts']} {r['kind']:<16} order={r['order_id']}"
              f" server={r['server_id']} {r['detail_json'][:160]}")

    print("\n=== final simulated fleet ===")
    state = exp.build_state(db, phase, end, cfg, apply_endings=False,
                            say=lambda *_: None)
    print(f"committed=${state['committed']:.2f} "
          f"remaining=${state['remaining']:.2f}")
    for r in state["rentals"]:
        print(f"  order={r['order_id']} sid={r['server_id']}"
              f" role={r['role']} its={r['its_total']}"
              f" hourly={r['hourly_usd']}"
              f" shares={sum(c for _, c in r['share_arrivals'])}")
    ended = db.execute(
        "SELECT order_id, server_id, end_reason, ended_at FROM"
        " experiment_rentals WHERE phase_id=? AND ended_at>=?",
        (phase_id, _iso(t0))).fetchall()
    print(f"ended in window: {len(ended)}")
    db.close()
    src.close()
    print(f"# replay db kept at {tmp}")


if __name__ == "__main__":
    main()
