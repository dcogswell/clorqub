#!/usr/bin/env python3
"""Read-only JSON API for the clorqub web dashboard.

Reads the same SQLite the TUI reads. No Clore API calls, no SSH, no writes.

Run:  .venv/bin/python web/api_app.py --db ~/.local/share/clorqub/clorqub.sqlite3
      then `cd web/react && npm run dev` and open http://localhost:5173
"""
import argparse
import json
import sys
import threading
import time
import uuid
from contextlib import closing
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from cli import DEFAULT_ENV
from tui_data import (DashboardError, _fleet_visible,
                      _history_stats_cached, open_readonly,
                      read_dashboard, read_experiment,
                      read_experiment_events, read_experiment_verdicts,
                      read_log_archive, read_server_details)

parser = argparse.ArgumentParser()
parser.add_argument("--db", required=True)
DB = parser.parse_known_args()[0].db
app = FastAPI(title="clorqub")

# In-memory rental flow state (single process; quotes expire quickly).
_rent_lock = threading.Lock()
_rent_flows = {}          # token -> {lines, done, rc, info, ctx}
_QUOTE_TTL_SECONDS = 600


def _rent_busy():
    """A create in flight blocks everything; a pending quote does not."""
    return any(f.get("started") and not f["done"]
               for f in _rent_flows.values())


def _flow_line(flow, msg):
    line = f"{datetime.now().strftime('%H:%M:%S')} {msg}"
    flow["lines"].append(line)


def clean(o):
    if isinstance(o, dict):
        return {k: clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean(v) for v in o]
    if isinstance(o, Decimal):
        return float(o)
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    if isinstance(o, set):
        return sorted(o)
    return o


def resp(o):
    return JSONResponse(clean(o))


def dashboard():
    try:
        return read_dashboard(DB)
    except DashboardError as exc:
        raise HTTPException(503, str(exc)) from exc


@app.get("/api/summary")
def summary():
    d = dashboard()
    exp = d.get("experiment") or {}
    ph = exp.get("phase") or {}
    return resp({
        "captured_at": d["captured_at"],
        "source": d["source"],
        "total": d["total"],
        "available": d["available"],
        "ranked": d["ranked"],
        "balance_usd": d["balance_usd"],
        "balance_at": d["balance_at"],
        "fleet_active": len(d["fleet"]),
        "orders_active": sum(1 for o in d["order_history"]
                             if o.get("state") == "active"),
        "shares_hr_total": sum(o.get("shares_hr") or 0
                               for o in d["order_history"]
                               if o.get("state") == "active"),
        "experiment": {
            "runner_alive": exp.get("runner_alive"),
            "phase": ph,
            "n_events": len(exp.get("events") or []),
            "n_candidates": len(exp.get("candidate_values") or []),
        },
    })


@app.get("/api/market")
def market():
    rows = []
    for r in dashboard()["rows"]:
        row = dict(r)
        row.pop("listing", None)
        rows.append(row)
    return resp(rows)


@app.get("/api/candidates")
def candidates():
    import experiment as exp_mod
    import market as market_mod
    exp = read_experiment(DB) or {}
    with closing(open_readonly(DB)) as db:
        stats = _history_stats_cached(db)
        # median on-demand ask per GPU over listings that could
        # actually be rented now -- the $ side of the prior table,
        # normalized per card like the its priors so a 2-card box's
        # whole-box price can't inflate the single-card read
        asks = {}
        for doc in (exp_mod.latest_listings(db)[0] or {}).values():
            gpu = market_mod.offered_gpu(doc)
            if gpu is None or not market_mod.available(doc):
                continue
            day = market_mod.price_per_day_usd(doc, "on-demand")
            gpu_n = len(doc.get("gpu_array") or []) or 1
            if day:
                asks.setdefault(gpu, []).append(day / 24.0 / gpu_n)
    gpu_stats = {g: dict(v)
                 for g, v in (stats.get("gpu_detail") or {}).items()}
    for gpu, vals in asks.items():
        row = gpu_stats.setdefault(gpu, {"prior": None, "proven": 0,
                                         "rentals": 0, "shares": 0,
                                         "weight": None, "weight_n": 0,
                                         "sol": 0.0,
                                         "cross_epoch": False})
        row["median_hr"] = exp_mod._median(vals)
        row["listings"] = len(vals)
    return resp({
        "candidate_values": exp.get("candidate_values") or [],
        "term_hours": exp.get("candidate_term_hours"),
        "value_model": ((exp.get("phase") or {}).get("value_model")),
        "gpu_stats": gpu_stats,
    })


@app.get("/api/server/{server_id}")
def server(server_id: int):
    try:
        return resp(read_server_details(DB, server_id))
    except DashboardError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/api/fleet")
def fleet():
    dash = dashboard()
    inventory = dash.get("order_inventory") or {}
    rigs = []
    for rig in dash["fleet_all"]:
        r = dict(rig)
        # the TUI's visibility rule, not just provision_state: an
        # observe-only rig whose order vanished from the last good
        # inventory poll is ended too
        r["visible"] = _fleet_visible(rig, inventory)
        r.pop("ssh_key_path", None)
        rigs.append(r)
    return resp(rigs)


@app.get("/api/rig/{order_id}/series")
def rig_series(order_id: int):
    """Parsed rig_observations time series for charts."""
    with closing(open_readonly(DB)) as db:
        if not db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table'"
                " AND name='rig_observations'").fetchone():
            return resp({"points": []})
        rows = db.execute(
            "SELECT captured_at, status_json, error_kind FROM rig_observations"
            " WHERE order_id=? ORDER BY id", (order_id,)).fetchall()
    points = []
    for row in rows:
        p = {"ts": row["captured_at"], "error": row["error_kind"]}
        if row["status_json"]:
            try:
                st = json.loads(row["status_json"])
                its = st.get("its") or {}
                sh = st.get("shares") or {}
                p.update({
                    "cpu_its": its.get("cpu"),
                    "gpu_its": its.get("gpu"),
                    "found": sh.get("found"),
                    "submitted": sh.get("submitted"),
                    "invalid": sh.get("invalid"),
                    "epoch": sh.get("epoch"),
                    "miner_running": (st.get("miner") or {}).get("running"),
                    "pool": st.get("pool"),
                })
            except (ValueError, TypeError, AttributeError):
                pass
        points.append(p)
    return resp({"points": points})


@app.get("/api/rig/{order_id}/spend")
def rig_spend(order_id: int):
    """Spend-over-time from order_inventory snapshots."""
    with closing(open_readonly(DB)) as db:
        if not db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table'"
                " AND name='order_inventory'").fetchone():
            return resp({"points": []})
        rows = db.execute(
            "SELECT captured_at, orders_json FROM order_inventory"
            " WHERE ok=1 ORDER BY id DESC LIMIT 400").fetchall()
    points = []
    for row in reversed(rows):
        try:
            orders = json.loads(row["orders_json"] or "[]")
        except (ValueError, TypeError):
            continue
        rec = next((o for o in orders
                    if isinstance(o, dict) and o.get("order_id") == order_id),
                   None)
        if rec is None:
            continue
        points.append({
            "ts": row["captured_at"],
            "spend": rec.get("spend_reported_native"),
            "price_day": rec.get("price_day_native"),
        })
    return resp({"points": points})


@app.get("/api/rig/{order_id}/log")
def rig_log(order_id: int, pool: str = "jetski", head: bool = False):
    return resp(read_log_archive(DB, order_id, pool, head=head))


@app.get("/api/orders")
def orders():
    return resp(dashboard()["order_history"])


@app.get("/api/experiment")
def experiment():
    return resp(read_experiment(DB))


@app.get("/api/experiment/status")
def experiment_status():
    """Cheap-enough header slice: phase pacing, runner liveness and
    in-flight bursts/rerents. Shares read_experiment's cached
    build_state pass with the rentals/candidates sections."""
    exp = read_experiment(DB) or {}
    return resp({
        "runner_alive": exp.get("runner_alive"),
        "phase": exp.get("phase"),
        "pending_rerents": exp.get("pending_rerents") or [],
        "pending_bursts": exp.get("pending_bursts") or [],
        "burst": exp.get("burst"),
        "bursts": exp.get("bursts") or [],
        "agent": exp.get("agent"),
        "offering_dead": exp.get("offering_dead"),
        "dead_trees": exp.get("dead_trees") or [],
        "tree_lifetime_min": exp.get("tree_lifetime_min"),
        "pool_share": exp.get("pool_share"),
    })


@app.get("/api/experiment/rentals")
def experiment_rentals():
    exp = read_experiment(DB) or {}
    return resp({"rentals": exp.get("rentals") or []})


@app.get("/api/experiment/events")
def experiment_events():
    return resp({"events": read_experiment_events(DB)})


@app.get("/api/experiment/verdicts")
def experiment_verdicts():
    return resp({"verdicts": read_experiment_verdicts(DB)})


_CONFIG_KEYS = ("score", "upgrade_margin", "promote_margin",
                "min_value_ratio", "min_return", "min_seat_return",
                "gpu_trial_min",
                "max_price_hr",
                "loss_mining", "paused", "probes_enabled",
                "slot_mode", "slot_min", "max_rentals",
                "growth_margin", "shrink_mode",
                "startup_min", "dry_after_min",
                "kill_lambda", "kill_lambda_free",
                "spot_enabled", "spot_probe_mode", "spot_slot_mode",
                "spot_bid_premium", "pool_share_enabled",
                "pool_share_target_pct", "pool_share_max_price_hr",
                "pool_share_reapprove_drift")


@app.get("/api/config")
def config():
    """Current phase's score yardstick + the editable gates."""
    import experiment as exp
    with closing(open_readonly(DB)) as db:
        phase = exp.open_phase(db, exp.utcnow())
        if phase is None:
            raise HTTPException(404, "no open experiment phase")
        cfg = exp.validate_config(json.loads(phase["config_json"]))
    return resp({k: cfg[k] for k in _CONFIG_KEYS}
                | {"score_help": exp.SCORE_HELP,
                   "strategies": sorted(exp.STRATEGIES)})


class ConfigRequest(BaseModel):
    model_config = {"extra": "forbid"}
    score: str | None = None
    upgrade_margin: float | None = None
    promote_margin: float | None = None
    min_value_ratio: float | None = None
    min_return: float | None = None
    min_seat_return: float | None = None
    gpu_trial_min: int | None = None
    max_price_hr: str | None = None
    loss_mining: bool | None = None
    paused: bool | None = None
    probes_enabled: bool | None = None
    slot_mode: str | None = None
    slot_min: int | None = None
    max_rentals: int | None = None
    growth_margin: float | None = None
    shrink_mode: str | None = None
    startup_min: int | None = None
    dry_after_min: int | None = None
    kill_lambda: float | None = None
    kill_lambda_free: float | None = None
    spot_enabled: bool | None = None
    spot_probe_mode: str | None = None
    spot_slot_mode: str | None = None
    spot_bid_premium: float | None = None
    pool_share_enabled: bool | None = None
    pool_share_target_pct: float | None = None
    pool_share_max_price_hr: str | None = None
    pool_share_reapprove_drift: float | None = None
    strategy: str | None = None


@app.post("/api/config")
def config_update(body: ConfigRequest):
    """Live config update; `strategy` expands a named preset first."""
    import experiment as exp
    updates = {}
    if body.strategy is not None:
        if body.strategy not in exp.STRATEGIES:
            raise HTTPException(
                422, "unknown strategy {!r} (want one of: {})".format(
                    body.strategy, ", ".join(sorted(exp.STRATEGIES))))
        updates.update(exp.STRATEGIES[body.strategy])
    for key in _CONFIG_KEYS:
        value = getattr(body, key)
        if value is not None:
            updates[key] = value
    if not updates:
        raise HTTPException(422, "nothing to update")
    db = exp.open_db(DB)
    try:
        try:
            cfg = exp.set_phase_config(db, **updates)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    finally:
        db.close()
    if cfg is None:
        raise HTTPException(404, "no open experiment phase")
    return resp({k: cfg[k] for k in _CONFIG_KEYS})


@app.post("/api/pool-share/approve")
def pool_share_approve():
    """Fund the current share-target estimate: snapshots the
    approved cost/pct into the phase config and sets the phase
    budget to committed + estimated epoch cost."""
    import experiment as exp
    db = exp.open_db(DB)
    try:
        try:
            result = exp.approve_pool_share(db)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    finally:
        db.close()
    return resp(result)


@app.get("/api/pool/series")
def pool_series():
    with closing(open_readonly(DB)) as db:
        if not db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table'"
                " AND name='pool_snapshots'").fetchone():
            return resp({"points": []})
        rows = db.execute(
            "SELECT source_ts, epoch, shares, solutions, counted,"
            " est_gains_usd, est_qus, usd_per_counted, qubic_price_usd,"
            " pool_sol_per_hour FROM pool_snapshots ORDER BY source_ts"
        ).fetchall()
    return resp({"points": [dict(r) for r in rows]})


@app.get("/api/pool/workers")
def pool_workers():
    with closing(open_readonly(DB)) as db:
        if not db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table'"
                " AND name='pool_worker_snapshots'").fetchone():
            return resp({"points": []})
        latest = db.execute(
            "SELECT MAX(snapshot_id) FROM pool_worker_snapshots").fetchone()[0]
        if latest is None:
            return resp({"points": []})
        rows = db.execute(
            "SELECT alias, sol, its, trainer, cuda, avx512, avx2, last_seen"
            " FROM pool_worker_snapshots WHERE snapshot_id=?", (latest,)
        ).fetchall()
    return resp({"points": [dict(r) for r in rows]})


class QuoteRequest(BaseModel):
    server_id: int
    hours: int
    max_cost_usd: str | None = None
    pool: str = "jetski"
    cpu_threads: int | None = None
    auto_renew: bool = False
    mode: str = "on-demand"
    spot_bid_premium: float | None = None


class ConfirmRequest(BaseModel):
    token: str


def _review_payload(ctx, spot_info=None):
    """Mirror the TUI ReviewModal fields for the web confirm modal."""
    q = ctx["quote"]
    server = ctx["server"] or {}
    specs = server.get("specs") or {}
    cap = ctx.get("max_cost_usd")
    import rent
    return {
        "server_id": ctx["server_id"],
        "alias": ctx["alias"],
        "pool": ctx["pool"],
        "gpu": ", ".join(specs.get("gpu_array") or []) or "—",
        "cpu": specs.get("cpu") or "—",
        "listed_threads": q.get("listed_cpu_allocation") or "?",
        "chosen_threads": q.get("cpu_threads"),
        "hours": q.get("hours"),
        "auto_renew": q.get("auto_renew") is True,
        "cost_bound_usd": q.get("cost_bound_usd"),
        "hourly_cost_usd": q.get("hourly_cost_usd"),
        "creation_fee_usd": q.get("creation_fee_usd"),
        "ceiling": None if cap in (None, "") else str(cap),
        "key_path": ctx["key_path"],
        "mode": q.get("mode") or "on-demand",
        "spot_bid_usd_day": q.get("spot_bid_usd_day"),
        "spot_floor_usd_day": q.get("spot_floor_usd_day"),
        "spot_top_bid_usd_day":
            spot_info.get("top_bid_usd_day") if spot_info else None,
        "spot_offers": spot_info.get("offers") if spot_info else None,
        "blacklist_warning": (
            rent.blacklist_warning(ctx["blacklist"])
            if ctx.get("blacklist") else None),
    }


def _prune_flows():
    now = time.time()
    for token, flow in list(_rent_flows.items()):
        if flow["done"] or now - flow["ts"] > _QUOTE_TTL_SECONDS:
            _rent_flows.pop(token, None)


@app.post("/api/rent/quote")
def rent_quote(body: QuoteRequest):
    """Phase 1: live quote via rent.quote_order; ctx held server-side."""
    _prune_flows()
    if _rent_busy():
        raise HTTPException(409, "another rental flow is in progress")
    if body.mode not in ("on-demand", "spot"):
        raise HTTPException(422, "mode must be on-demand or spot")
    import rent
    lines = []

    def collect(msg):
        lines.append(str(msg))

    spot_info = None
    if body.mode == "spot":
        # mirror the experiment's burst path: a fresh spot-market
        # quote, then bid = floor or premium x top competing USD bid
        import spot
        from experiment import DEFAULT_CONFIG, utcnow
        premium = (body.spot_bid_premium
                   if body.spot_bid_premium is not None
                   else DEFAULT_CONFIG["spot_bid_premium"])
        try:
            api_key, _ = rent.load_secrets(DEFAULT_ENV, need_creds=False)
            now = utcnow()
            spot_info = spot.bid_for(
                {"captured_at": now.isoformat(),
                 "raw_json": json.dumps(
                     spot.fetch_quote(api_key, body.server_id)),
                 "error_kind": None}, now, premium=premium)
        except Exception:
            spot_info = None
        if spot_info is None:
            raise HTTPException(
                422, "no usable USD-Blockchain spot quote for this server")
        collect(f"spot: bid ${spot_info['bid_usd_day']}/day "
                f"(floor ${spot_info['floor_usd_day']}, top competing "
                f"{spot_info['top_bid_usd_day']}, "
                f"{spot_info['offers']} offers, premium {premium})")

    try:
        ctx = rent.quote_order(
            server_id=body.server_id, hours=body.hours,
            auto_renew=body.auto_renew,
            max_cost_usd=body.max_cost_usd,
            pool=body.pool, cpu_threads=body.cpu_threads,
            db_path=DB, env_file=DEFAULT_ENV,
            key_path=rent.DEFAULT_KEY,
            mode=body.mode,
            spot_bid_usd_day=(spot_info["bid_usd_day"]
                              if spot_info else None),
            spot_floor_usd_day=(spot_info["floor_usd_day"]
                                if spot_info else None),
            say=collect, warn=collect)
    except (ValueError, rent.APIError, OSError) as exc:
        raise HTTPException(422, f"quote failed: {exc}") from exc
    except Exception as exc:
        raise HTTPException(500, f"quote failed: {type(exc).__name__}") from exc
    token = uuid.uuid4().hex
    _rent_flows[token] = {"ctx": ctx, "lines": lines, "done": False,
                          "rc": None, "info": None, "ts": time.time()}
    return resp({"token": token,
                 "review": _review_payload(ctx, spot_info),
                 "lines": lines})


@app.post("/api/rent/confirm")
def rent_confirm(body: ConfirmRequest):
    """Phase 2: run create_order_flow in a background thread."""
    flow = _rent_flows.get(body.token)
    if flow is None:
        raise HTTPException(404, "quote expired; request a new one")
    with _rent_lock:
        if flow.get("started"):
            return resp({"started": True})
        flow["started"] = True
    flow["lines"].append(
        "confirmed -- re-verifying terms, then ONE create_order POST")

    def run():
        import rent

        def collect(msg):
            _flow_line(flow, msg)

        try:
            rc, info = rent.create_order_flow(flow["ctx"],
                                              say=collect, warn=collect)
        except Exception as exc:  # last-resort guard; type name only
            rc, info = 1, {"order_id": None, "provision_state": None,
                           "stage": type(exc).__name__}
        flow["rc"], flow["info"], flow["done"] = rc, info, True

    threading.Thread(target=run, daemon=True).start()
    return resp({"started": True, "token": body.token})


@app.get("/api/rent/status/{token}")
def rent_status(token: str):
    flow = _rent_flows.get(token)
    if flow is None:
        raise HTTPException(404, "unknown or expired token")
    return resp({"lines": flow["lines"], "done": flow["done"],
                 "rc": flow["rc"], "info": flow["info"]})


@app.post("/api/rent/cancel-quote")
def rent_cancel_quote(body: ConfirmRequest):
    _rent_flows.pop(body.token, None)
    return resp({"dropped": True})


class PhaseStartRequest(BaseModel):
    budget_usd: str
    phase_hours: float | None = None
    until_epoch_end: bool = False
    reserve_usd: str | None = None


def _phase_info(r, now_iso):
    return {**dict(r),
            "active": r["closed_at"] is None and r["ends_at"] > now_iso}


@app.get("/api/phase")
def phase_status():
    """Open phase + recent history + next epoch boundary."""
    import experiment as exp
    now = exp.utcnow()
    now_iso = exp._iso(now)
    with closing(open_readonly(DB)) as db:
        rows = db.execute(
            "SELECT phase_id, started_at, ends_at, budget_usd, "
            "reserve_usd, closed_at FROM experiment_phases "
            "ORDER BY phase_id DESC LIMIT 8").fetchall()
    open_row = next((r for r in rows if r["closed_at"] is None), None)
    return resp({
        "open": _phase_info(open_row, now_iso) if open_row else None,
        "recent": [_phase_info(r, now_iso) for r in rows],
        "epoch_ends_at": exp._iso(exp.epoch_end(now))})


@app.post("/api/phase/start")
def phase_start(body: PhaseStartRequest):
    """Mirror cmd_start_phase: insert a phase row; the runner binds it
    on its next launch (a running loop keeps its captured phase)."""
    import experiment as exp
    db = exp.open_db(DB)
    try:
        now = exp.utcnow()
        existing = db.execute(
            "SELECT phase_id, ends_at FROM experiment_phases WHERE "
            "closed_at IS NULL AND ends_at > ?",
            (exp._iso(now),)).fetchone()
        if existing is not None:
            raise HTTPException(
                409, f"phase {existing['phase_id']} is still open "
                     f"(ends {existing['ends_at']})")
        try:
            budget = exp._dec(body.budget_usd)
        except Exception:
            raise HTTPException(422, "budget_usd must be a USD amount")
        if budget <= 0:
            raise HTTPException(422, "budget_usd must be positive")
        try:
            reserve = exp._dec(
                body.reserve_usd if body.reserve_usd not in (None, "")
                else "0.50")
        except Exception:
            raise HTTPException(422, "reserve_usd must be a USD amount")
        if reserve < 0:
            raise HTTPException(422, "reserve_usd must be >= 0")
        if body.until_epoch_end:
            ends_at = exp.epoch_end(now)
        else:
            hours = (body.phase_hours
                     if body.phase_hours is not None else 48.0)
            if isinstance(hours, bool) or not 0 < hours <= 24 * 31:
                raise HTTPException(
                    422, "phase_hours must be 0 < h <= 744")
            ends_at = now + timedelta(hours=hours)
        cfg = exp.validate_config(dict(exp.DEFAULT_CONFIG))
        with db:
            cur = db.execute(
                "INSERT INTO experiment_phases(started_at, ends_at, "
                "budget_usd, reserve_usd, config_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (exp._iso(now), exp._iso(ends_at), str(budget),
                 str(reserve), json.dumps(cfg, sort_keys=True)))
        return resp({"phase_id": cur.lastrowid,
                     "started_at": exp._iso(now),
                     "ends_at": exp._iso(ends_at)})
    finally:
        db.close()


@app.post("/api/phase/close")
def phase_close():
    """Close the open phase and pause its config so a still-running
    loop halts new actions on its next _live_cfg read (phase
    boundaries never cancel live rentals -- they run to term)."""
    import experiment as exp
    db = exp.open_db(DB)
    try:
        phase = exp.open_phase(db, exp.utcnow())
        if phase is None:
            raise HTTPException(404, "no open phase")
        try:
            cfg = exp.validate_config(json.loads(phase["config_json"]))
        except ValueError:
            cfg = dict(exp.DEFAULT_CONFIG)
        cfg["paused"] = True
        with db:
            db.execute(
                "UPDATE experiment_phases SET closed_at=?, "
                "config_json=? WHERE phase_id=?",
                (exp._iso(exp.utcnow()), json.dumps(cfg, sort_keys=True),
                 phase["phase_id"]))
        return resp({"closed": True, "phase_id": phase["phase_id"]})
    finally:
        db.close()


class PinRequest(BaseModel):
    server_id: int
    pinned: bool = True


@app.get("/api/probe-pins")
def get_probe_pins():
    """Operator-tagged servers for the open phase, in tag order."""
    import experiment as exp
    with closing(open_readonly(DB)) as db:
        phase = exp.open_phase(db, exp.utcnow())
        if phase is None:
            return resp({"pins": [], "phase_id": None})
        rows = db.execute(
            "SELECT server_id, tagged_at FROM probe_pins "
            "WHERE phase_id=? ORDER BY tagged_at, server_id",
            (phase["phase_id"],)).fetchall()
    return resp({"pins": [dict(r) for r in rows],
                 "phase_id": phase["phase_id"]})


@app.post("/api/probe-pins")
def set_probe_pin(body: PinRequest):
    import experiment as exp
    db = exp.open_db(DB)
    try:
        phase = exp.open_phase(db, exp.utcnow())
        if phase is None:
            raise HTTPException(404, "no open experiment phase")
        exp.set_probe_pin(db, phase["phase_id"], body.server_id,
                          body.pinned)
        return resp({"pins": exp.probe_pins(db, phase["phase_id"])})
    finally:
        db.close()


class TreeReportRequest(BaseModel):
    source: str
    tree_id: str
    epoch: int | None = None
    seed: str | None = None
    its: float | None = None
    shares: int | None = None
    detail: dict | None = None


@app.post("/api/tree/report")
def tree_report(body: TreeReportRequest):
    """External tree observation: a scout miner (e.g. a home-PC rig)
    reports which tree the pool assigned it -- the same draw a new
    rental would get right now, without paying for a probe."""
    import experiment as exp
    db = exp.open_db(DB)
    try:
        with db:
            db.execute(
                "INSERT INTO tree_reports(ts, source, epoch, tree_id,"
                " seed, its, shares, detail_json)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (exp._iso(exp.utcnow()), body.source, body.epoch,
                 body.tree_id, body.seed, body.its, body.shares,
                 json.dumps(body.detail or {})))
        return resp({"ok": True})
    finally:
        db.close()


@app.get("/api/tree/status")
def tree_status():
    """Latest scout report + the fleet's dead-tree registry."""
    import experiment as exp
    with closing(open_readonly(DB)) as db:
        row = db.execute(
            "SELECT ts, source, epoch, tree_id, seed, its, shares"
            " FROM tree_reports ORDER BY id DESC LIMIT 1").fetchone()
        phase = exp.open_phase(db, exp.utcnow())
        dead, life = (exp._tree_stats(db, phase["phase_id"])
                      if phase else (set(), None))
    return resp({
        "latest": dict(row) if row else None,
        "dead_trees": sorted(dead),
        "tree_lifetime_min": life})


class CancelRequest(BaseModel):
    order_id: int


@app.post("/api/orders/cancel")
def cancel_order(body: CancelRequest):
    """Cancel one locally managed order (one POST, no retry)."""
    import rent
    lines = []
    try:
        rc, info = rent.cancel_managed_order(
            body.order_id, db_path=DB, env_file=DEFAULT_ENV,
            say=lambda m: lines.append(str(m)),
            warn=lambda m: lines.append(str(m)))
    except (ValueError, rent.APIError, OSError) as exc:
        raise HTTPException(422, f"cancel failed: {exc}") from exc
    except Exception as exc:
        raise HTTPException(500, f"cancel failed: {type(exc).__name__}") \
            from exc
    return resp({"rc": rc, "info": info, "lines": lines})


@app.get("/api/blacklist")
def blacklist():
    with closing(open_readonly(DB)) as db:
        if not db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table'"
                " AND name='blacklist'").fetchone():
            return resp([])
        rows = db.execute(
            "SELECT * FROM blacklist ORDER BY added_at DESC").fetchall()
    return resp([dict(r) for r in rows])


class BlacklistRemoveRequest(BaseModel):
    server_id: int


@app.post("/api/blacklist/remove")
def blacklist_remove(body: BlacklistRemoveRequest):
    """Clear all active blacklist entries for a server (manual undo)."""
    import experiment as exp
    db = exp.open_db(DB)
    try:
        import blacklist as bl
        return resp({"removed": bl.remove(db, body.server_id)})
    finally:
        db.close()


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8083)
