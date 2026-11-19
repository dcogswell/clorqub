"""Read-only SQLite views for the TUI; no credentials or remote APIs."""

import json
import sqlite3
import threading
import time
from collections import Counter
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import blacklist
import experiment
import fleet_store
from market import RENTER_FEES, available, cuda_capable, score_listing
from orders import latest_wallets, read_inventory
from spot import bid_for, latest_quotes, summarize_quote
from telemetry_policy import health, timestamp


class DashboardError(Exception):
    """A missing, incompatible, or temporarily unreadable dashboard database."""


def read_orders_history(db):
    """Every order ever seen in a good snapshot; newest record per id."""
    if not db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='order_inventory'"
    ).fetchone():
        return []
    records, first_seen, last_seen = {}, {}, {}
    active_ids = None
    for row in db.execute(
            "SELECT captured_at, orders_json FROM order_inventory "
            "WHERE ok=1 ORDER BY id DESC"):
        try:
            orders = json.loads(row["orders_json"] or "[]")
        except (ValueError, TypeError):
            continue
        if not isinstance(orders, list):
            continue
        if active_ids is None:
            active_ids = {o.get("order_id") for o in orders
                          if isinstance(o, dict)}
        for order in orders:
            if not isinstance(order, dict):
                continue
            oid = order.get("order_id")
            if oid is None:
                continue
            if oid not in records:
                records[oid] = order
                last_seen[oid] = row["captured_at"]
            first_seen[oid] = row["captured_at"]
    result = []
    for oid, record in records.items():
        item = dict(record)
        item["state"] = "active" if oid in (active_ids or ()) else "ended"
        item["first_seen"] = first_seen[oid]
        item["last_seen"] = last_seen[oid]
        result.append(item)
    result.sort(key=lambda o: o.get("last_seen") or "", reverse=True)
    return result


def _fleet_visible(rig, inventory):
    """Hide dead rigs; keep any the inventory cannot prove ended."""
    if rig.get("provision_state") in ("cancelled", "missing",
                                      "provision_failed"):
        return False
    if not inventory.get("ok"):
        return True
    try:
        captured = timestamp(inventory["captured_at"])
        created = timestamp(rig["created_at"])
    except (TypeError, ValueError, KeyError):
        return True
    if captured <= created:
        return True  # inventory predates enrollment
    return rig.get("order_id") in {
        o.get("order_id") for o in inventory.get("orders") or []}


def _order_modes(inventory):
    """order_id -> rental mode from the last good inventory: 'spot' /
    'on-demand', or None when Clore reported no spot flag."""
    modes = {}
    for o in (inventory or {}).get("orders") or []:
        if isinstance(o, dict):
            spot = o.get("spot")
            modes[o.get("order_id")] = (
                "spot" if spot is True else
                "on-demand" if spot is False else None)
    return modes


def _shares_per_hr(db, order_id):
    """Submitted shares/hour across recent successful polls; None if n/a."""
    rows = db.execute(
        "SELECT captured_at, status_json FROM rig_observations"
        " WHERE order_id=? AND status_json IS NOT NULL"
        " ORDER BY id DESC LIMIT 150", (order_id,)).fetchall()
    samples = []
    for row in rows:
        try:
            shares = json.loads(row["status_json"]).get("shares") or {}
            submitted = shares.get("submitted")
            if not isinstance(submitted, (int, float)):
                continue
            samples.append((timestamp(row["captured_at"]),
                            submitted, shares.get("epoch")))
        except (ValueError, TypeError):
            continue
        if len(samples) >= 2 and \
                (samples[0][0] - samples[-1][0]).total_seconds() >= 3300:
            break
    if len(samples) < 2:
        return None
    (t_new, s_new, e_new), (t_old, s_old, e_old) = samples[0], samples[-1]
    span = (t_new - t_old).total_seconds()
    if span < 120 or e_new != e_old or s_new < s_old:
        return None
    return (s_new - s_old) * 3600.0 / span


def _its_per_order(db, order_id):
    """Median measured it/s across the order's good observations,
    skipping the 2-min post-t0 warmup like the runner's smoothed
    its_total; None when the rig was never reached."""
    good = []
    for row in db.execute(
            "SELECT captured_at, status_json FROM rig_observations"
            " WHERE order_id=? AND status_json IS NOT NULL"
            " ORDER BY id", (order_id,)):
        try:
            good.append((timestamp(row["captured_at"]),
                         json.loads(row["status_json"])))
        except (ValueError, TypeError):
            continue
    if not good:
        return None
    t0 = good[0][0]
    post = [s for ts, s in good if ts >= t0 + timedelta(minutes=2)]
    return experiment._median(
        [experiment._its_total(s) for s in (post or [s for _, s in good])])


def _last_share_at(db, order_id):
    """ISO time of the last share arrival under the runner's
    cumulative-counter rules (experiment.share_arrivals); None when
    no share has ever been observed. Distinct from observed_at,
    which only says the rig answered a poll."""
    good = []
    for row in db.execute(
            "SELECT captured_at, status_json FROM rig_observations"
            " WHERE order_id=? AND status_json IS NOT NULL"
            " ORDER BY id", (order_id,)):
        try:
            good.append((timestamp(row["captured_at"]),
                         json.loads(row["status_json"])))
        except (ValueError, TypeError):
            continue
    arrivals = experiment.share_arrivals(good)
    return arrivals[-1][0].isoformat() if arrivals else None


def _shares_per_cent(order):
    """Shares/hr per fee-inclusive cent/hr; None if price/shares n/a."""
    shares = order.get("shares_hr")
    price = order.get("price_day_native")
    if not isinstance(shares, (int, float)) or isinstance(shares, bool) \
            or not isinstance(price, (int, float)) \
            or isinstance(price, bool) or price <= 0:
        return None
    fee = 1.0125 if order.get("spot") is True else 1.05
    return shares / (price / 24 * fee * 100)


def _usd_balance(wallets):
    for w in wallets:
        if "USD" in str(w.get("name") or ""):
            return w.get("balance")
    return None


def _rig_cost(rig, order_index, price_index=None):
    """Observed Clore renter fees: 1.25% spot, 5% on-demand."""
    record = order_index.get(rig.get("order_id"))
    rate, spend = None, None
    if record is not None:
        rig["order_gpus"] = record.get("gpus")
        rig["spend_reported_native"] = record.get(
            "spend_reported_native")
        rig["online_reported"] = record.get("online_reported")
        if record.get("auto_renew") is not None:
            rig["auto_renew"] = 1 if record["auto_renew"] is True else 0
        end_unix = record.get("rental_end")
        if isinstance(end_unix, int) and not isinstance(end_unix, bool) \
                and end_unix > 0:
            rig["end_at"] = datetime.fromtimestamp(
                end_unix, timezone.utc).isoformat(timespec="seconds")
        spend = record.get("spend_reported_native")
        price = record.get("price_day_native")
        rig["price_day_native"] = price
        if price is not None:
            fee = 1.0125 if record.get("spot") is True else 1.05
            rate = price / 24 * fee
    if rate is None and price_index is not None:
        price = price_index.get(rig.get("server_id"))
        if price is not None:
            rate = price / 24 * 1.05
    if rate is None:
        try:
            hours = (timestamp(rig["deadline_at"])
                     - timestamp(rig["created_at"])).total_seconds() / 3600
            if hours > 0:
                rate = float(rig["quoted_cost_usd"]) / hours
        except (TypeError, ValueError, KeyError):
            pass
    rig["rate_hr_usd"] = rate
    rig["spend_usd"] = spend
    rig.setdefault("end_at", rig.get("deadline_at"))
    until = rig.get("end_at")
    if record is not None and record.get("state") == "ended":
        # early-cancelled orders keep a future rental_end; the last
        # snapshot still listing the order bounds actual uptime
        try:
            if until is None \
                    or timestamp(record["last_seen"]) < timestamp(until):
                until = record["last_seen"]
        except (TypeError, ValueError, KeyError):
            pass
    rig["up_until"] = until


def open_readonly(path):
    db = sqlite3.connect(
        Path(path).expanduser().resolve().as_uri() + "?mode=ro", uri=True, timeout=1
    )
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    return db


_STATS_CACHE = {}


def _history_stats_cached(db):
    """Measured hardware priors, recomputed only when new observations land."""
    key = db.execute(
        "SELECT COALESCE(MAX(id),0) FROM rig_observations"
    ).fetchone()[0], db.execute(
        "SELECT COUNT(*) FROM fleet_rentals"
    ).fetchone()[0], db.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
        " AND name='pool_worker_snapshots'").fetchone()[0] and db.execute(
        "SELECT COUNT(*) FROM pool_worker_snapshots").fetchone()[0]
    if _STATS_CACHE.get("key") == key:
        return _STATS_CACHE["stats"]
    ids = [r[0] for r in db.execute(
        "SELECT DISTINCT server_id FROM fleet_rentals")]
    known = {}
    if ids:
        marks = ",".join("?" * len(ids))
        for sid, js in db.execute(
                "SELECT server_id, latest_json FROM servers "
                f"WHERE server_id IN ({marks})", ids):
            known[sid] = json.loads(js)
    stats = experiment.history_stats(db, known)
    _STATS_CACHE.update(key=key, stats=stats)
    return stats


_exp_lock = threading.Lock()
_exp_cache = {"path": None, "at": 0.0, "data": None}
_EXP_CACHE_TTL = 10.0


def read_experiment(path):
    """experiment.summary() behind a short TTL: every section
    endpoint needs the same build_state pass, so parallel calls share
    one result instead of each recomputing it."""
    with _exp_lock:
        if _exp_cache["path"] == path \
                and time.time() - _exp_cache["at"] < _EXP_CACHE_TTL:
            return _exp_cache["data"]
        with closing(open_readonly(path)) as db:
            try:
                data = experiment.summary(
                    db, datetime.now(timezone.utc), db_path=path)
            except Exception:
                data = None
        _exp_cache.update(path=path, data=data, at=time.time())
        return data


def read_experiment_events(path, limit=200):
    """The event log without build_state: the cheapest section of the
    experiment summary, for views that only need the log."""
    with closing(open_readonly(path)) as db:
        if db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table'"
                " AND name='experiment_phases'").fetchone() is None:
            return []
        phase = db.execute(
            "SELECT phase_id FROM experiment_phases"
            " WHERE closed_at IS NULL ORDER BY phase_id DESC LIMIT 1"
        ).fetchone() or db.execute(
            "SELECT phase_id FROM experiment_phases"
            " ORDER BY phase_id DESC LIMIT 1").fetchone()
        if phase is None:
            return []
        return [{
            "id": e["id"], "ts": e["ts"], "kind": e["kind"],
            "order_id": e["order_id"], "server_id": e["server_id"],
            "detail": json.loads(e["detail_json"])}
            for e in db.execute(
                "SELECT * FROM experiment_events WHERE phase_id=?"
                " ORDER BY id DESC LIMIT ?",
                (phase["phase_id"], limit))]


def _open_or_latest_phase_id(db):
    return db.execute(
        "SELECT phase_id FROM experiment_phases"
        " WHERE closed_at IS NULL ORDER BY phase_id DESC LIMIT 1"
    ).fetchone() or db.execute(
        "SELECT phase_id FROM experiment_phases"
        " ORDER BY phase_id DESC LIMIT 1").fetchone()


def read_experiment_verdicts(path, limit=200):
    """One row per decided outcome, newest first: ended rentals with
    their cancel evidence plus the burst verdict the rental took part
    in, so 'what was decided and on what numbers' reads without
    cross-referencing the raw event stream."""
    with closing(open_readonly(path)) as db:
        if db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table'"
                " AND name='experiment_rentals'").fetchone() is None:
            return []
        phase = _open_or_latest_phase_id(db)
        if phase is None:
            return []
        # order -> burst outcome text, from burst_judged details
        order_outcome = {}
        for b in db.execute(
                "SELECT detail_json FROM experiment_events"
                " WHERE phase_id=? AND kind='burst_judged' ORDER BY id",
                (phase["phase_id"],)):
            d = json.loads(b["detail_json"])
            st = d.get("stats") or {}
            bid, verdict = d.get("burst_id"), d.get("verdict")
            bits = []
            if st.get("probe_its") is not None:
                bits.append(f"probe {st['probe_its']:.0f}its")
            if st.get("probe_net_usd_hr") is not None:
                bits.append(f"${st['probe_net_usd_hr']:.2f}/h")
            if st.get("orig_its") is not None:
                bits.append(f"vs {st['orig_its']:.0f}its")
            if st.get("orig_net_usd_hr") is not None:
                bits.append(f"${st['orig_net_usd_hr']:.2f}/h")
            text = (f"burst {bid} · {verdict}"
                    + (f" ({' '.join(bits)})" if bits else ""))
            for oid in (d.get("finders") or []) + \
                    (d.get("cancelled") or []) + \
                    [d.get("promote"), d.get("cancel_original")]:
                if oid is not None and oid not in order_outcome:
                    order_outcome[oid] = text
        rows = []
        for r in db.execute(
                "SELECT r.order_id, r.server_id, r.role, r.mode,"
                " r.tier, r.created_at, r.ended_at, r.end_reason,"
                " r.hourly_usd, r.quoted_cost_usd,"
                " (SELECT e.detail_json FROM experiment_events e"
                "  WHERE e.order_id=r.order_id AND e.kind='cancel'"
                "  ORDER BY e.id DESC LIMIT 1) AS cancel_json"
                " FROM experiment_rentals r"
                " WHERE r.phase_id=? AND r.ended_at IS NOT NULL"
                " ORDER BY r.ended_at DESC LIMIT ?",
                (phase["phase_id"], limit)):
            cd = json.loads(r["cancel_json"]) if r["cancel_json"] else {}
            rows.append({
                "order_id": r["order_id"], "server_id": r["server_id"],
                "role": r["role"], "mode": r["mode"],
                "tier": r["tier"], "ended_at": r["ended_at"],
                "verdict": r["end_reason"],
                "decided": cd.get("decided"),
                "its": cd.get("its"), "shares": cd.get("shares"),
                "age_min": cd.get("age_min"),
                "cost_usd": float(r["quoted_cost_usd"] or 0),
                "outcome": order_outcome.get(r["order_id"]),
            })
        return rows


def read_dashboard(path):
    """Latest imported market only: disappeared listings must not remain candidates."""
    try:
        with closing(open_readonly(path)) as db:
            latest = db.execute(
                "SELECT id, captured_at, source, raw_json FROM marketplace_snapshots "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
            benchmarks = [dict(row) for row in db.execute(
                "SELECT gpu_model, cpu_profile, cpu_isa, pool, observation_kind, "
                "min_sols_hr, max_sols_hr, source, thread_count, epoch, "
                "trainer_version, notes FROM performance_benchmarks ORDER BY gpu_model"
            )]
            observations = [json.loads(row["raw_json"]) for row in db.execute(
                "SELECT raw_json FROM performance_observations ORDER BY observation_id"
            )]
            quotes = latest_quotes(db)
            inventory = read_inventory(db)
            history = read_orders_history(db)
            if db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table'"
                    " AND name='rig_observations'").fetchone():
                for o in history:
                    o["shares_hr"] = _shares_per_hr(db, o["order_id"])
                    o["shares_per_cent"] = _shares_per_cent(o)
            order_index = {o["order_id"]: o for o in history}
            fleet_all = fleet_store.read_fleet(db)
            price_index = {}
            if db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table'"
                    " AND name='server_prices'").fetchone():
                price_index = {row["server_id"]:
                               row["on_demand_usd_day"] for row in
                               db.execute(
                                   "SELECT server_id, on_demand_usd_day"
                                   " FROM server_prices"
                                   " WHERE valid_to IS NULL")}
            cc_map = {}
            if db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table'"
                    " AND name='server_specs'").fetchone():
                cc_map = {row["server_id"]: row["cc"] for row in
                          db.execute(
                              "SELECT server_id,"
                              " json_extract(net_json,'$.cc') AS cc"
                              " FROM server_specs"
                              " WHERE valid_to IS NULL")}
            for rig in fleet_all:
                rig["country"] = cc_map.get(rig.get("server_id"))
                rig["health"] = health(rig)
                rig["shares_hr"] = _shares_per_hr(db, rig["order_id"])
                rig["last_share_at"] = _last_share_at(
                    db, rig["order_id"])
                _rig_cost(rig, order_index, price_index)
            # Clore orders not enrolled as rigs (e.g. manual web-UI
            # rentals): minimal rows so Fleet still shows them
            fleet_ids = {rig["order_id"] for rig in fleet_all}
            for o in history:
                if o["order_id"] in fleet_ids:
                    continue
                price = o.get("price_day_native")
                rate = None
                if isinstance(price, (int, float)) \
                        and not isinstance(price, bool):
                    fee = (1.0125 if o.get("spot") is True else 1.05)
                    rate = price / 24 * fee
                fleet_all.append({
                    "order_id": o["order_id"],
                    "server_id": o.get("server_id"),
                    "unmanaged": True,
                    "health": "unmanaged",
                    "spend_reported_native":
                        o.get("spend_reported_native"),
                    "spend_usd": o.get("spend_reported_native"),
                    "online_reported": o.get("online_reported"),
                    "price_day_native": price,
                    "rate_hr_usd": rate,
                    "gpus": o.get("gpus"),
                    "order_gpus": o.get("gpus"),
                    "created_at": None,
                    "provision_state": (
                        "ready" if o.get("state") == "active"
                        else "cancelled"),
                    "state": o.get("state"),
                })
            # order_inventory's own control field only describes the
            # read-only poll; label who actually rents each rig
            exp_ids = set()
            if db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table'"
                    " AND name='experiment_rentals'").fetchone():
                exp_ids = {row["order_id"] for row in db.execute(
                    "SELECT order_id FROM experiment_rentals")}
            modes = _order_modes(inventory)
            for rig in fleet_all:
                # an order the last good inventory no longer lists is
                # presumed on-demand rather than unknown
                rig["mode"] = modes.get(rig.get("order_id"), "on-demand")
                if rig.get("unmanaged"):
                    rig["control"] = "unmanaged"
                elif rig["order_id"] in exp_ids:
                    rig["control"] = "experiment"
                elif rig.get("provision_state") == "observe-only":
                    rig["control"] = "enrolled"
                else:
                    rig["control"] = "managed"
            fleet = [rig for rig in fleet_all
                     if _fleet_visible(rig, inventory)]
            wallets = latest_wallets(db)
            exp = read_experiment(path)
            blocked = blacklist.active(db)
            try:
                stats = _history_stats_cached(db)
            except Exception:
                stats = None
            # orders seen on this account per server — "previously
            # rented" for the Candidates/Marketplace quick filters
            prior_counts = Counter(
                o["server_id"] for o in history if o.get("server_id"))
        listings = json.loads(latest["raw_json"])["servers"] if latest else []
        # the experiment's static eligibility gates, so the Candidates
        # tab shows the same pool auto-rental/burst/upgrade draws from
        ph = (exp or {}).get("phase") or {}
        min_rel = ph.get(
            "min_reliability",
            experiment.DEFAULT_CONFIG["min_reliability"])
        max_price = ph.get(
            "max_price_hr",
            float(experiment._dec(
                experiment.DEFAULT_CONFIG["max_price_hr"])))
        rows = []
        cuda_hidden = 0
        for listing in listings:
            if not cuda_capable(listing):
                cuda_hidden += 1
                continue
            row = score_listing(listing)
            entry = blocked.get(row["server_id"])
            if entry:
                row["eligible"] = False
                row["ineligible_reason"] = f"blacklisted: {entry['reason']}"
                row["blacklist"] = entry
            if row["eligible"]:
                rel = row.get("reliability")
                if min_rel and (rel is None or rel < min_rel):
                    row["eligible"] = False
                    row["ineligible_reason"] = \
                        f"reliability < {min_rel * 100:g}%"
                elif (row["hourly_cost_usd"] is not None
                        and row["hourly_cost_usd"] > max_price):
                    row["eligible"] = False
                    row["ineligible_reason"] = \
                        f"over ${max_price:.2f}/h cap"
            row["listing"] = listing
            row["available"] = available(listing)
            row["prior_rentals"] = prior_counts.get(row["server_id"], 0)
            row["spot"] = summarize_quote(quotes.get(row["server_id"]))
            bid = bid_for(quotes.get(row["server_id"]))
            od = row.get("hourly_cost_usd")
            if bid is not None and isinstance(od, (int, float)) \
                    and not isinstance(od, bool) and od > 0:
                row["spot_discount"] = 1 - (
                    bid["bid_usd_day"] / 24
                    * (1 + RENTER_FEES["spot"])) / float(od)
            rows.append(row)
        if stats:
            for row in rows:
                prior, extrapolated, cross_ep = experiment.est_its(
                    stats, row["server_id"], row["cpu"], row["gpu"],
                    gpu_count=row.get("gpu_count") or 1)
                if prior:
                    row["est_its"] = prior
                    row["est_extrapolated"] = extrapolated
                    row["est_cross_epoch"] = cross_ep
                    if row["hourly_cost_usd"]:
                        row["est_its_per_usd"] = \
                            prior / float(row["hourly_cost_usd"])
        # when an experiment phase is live, the runner's own scored
        # pool decides Candidates membership: it keeps rows the static
        # gates pass (incl. "no matching benchmark" estimates) so an
        # economically rejected candidate still shows with its Auto
        # status; non-members stay in the Marketplace.
        auto_pool = None
        if exp:
            cand_vals = exp.get("candidate_values")
            if cand_vals is not None:
                auto_pool = {cv["server_id"]: cv
                             for cv in cand_vals}
        if auto_pool is not None:
            for row in rows:
                cv = auto_pool.get(row["server_id"])
                if cv is None:
                    row["eligible"] = False
                    if not row.get("ineligible_reason"):
                        row["ineligible_reason"] = "not in auto pool"
                    continue
                row["auto_prior"] = cv["prior"]
                row["forecast_usd_hr"] = cv["usd_hr"]
                row["forecast_return_x"] = cv["return_x"]
                row["forecast_net_hr"] = cv.get("net_hr")
                row["value_fill_status"] = cv["fill_status"]
                row["upgrade_status"] = cv.get("upgrade_status")
                row["mode"] = cv.get("mode")
                row["od_hourly"] = cv.get("od_hourly")
                row["spot_hourly"] = cv.get("spot_hourly")
                row["spot_discount"] = (
                    cv.get("spot_discount") or row.get("spot_discount"))
                row["eligible"] = True
                row["ineligible_reason"] = None
                row["est_its"] = cv["est_its"]
                row["est_extrapolated"] = cv["extrapolated"]
                row["est_cross_epoch"] = cv.get("cross_epoch")
                if cv["est_its"] and row["hourly_cost_usd"]:
                    row["est_its_per_usd"] = \
                        cv["est_its"] / float(row["hourly_cost_usd"])
                else:
                    row.pop("est_its_per_usd", None)
        return {
            "blacklist_signature": tuple(
                (sid, e["reason"], e["source"], e["added_at"], e["expires_at"])
                for sid, e in sorted(blocked.items())),
            "snapshot_id": latest["id"] if latest else None,
            "captured_at": latest["captured_at"] if latest else None,
            "source": latest["source"] if latest else None,
            "rows": rows,
            "total": len(rows),
            "cuda_hidden": cuda_hidden,
            "available": sum(row["available"] for row in rows),
            "ranked": sum(row["eligible"] for row in rows),
            "benchmarks": benchmarks,
            "observations": observations,
            "spot_signature": tuple(
                (row["server_id"], quotes[row["server_id"]]["id"], row["spot"]["status"])
                for row in rows if row["server_id"] in quotes
            ),
            "order_inventory": inventory,
            "balance_usd": _usd_balance(wallets["wallets"]),
            "balance_at": wallets["captured_at"],
            "order_history": history,
            "order_index": order_index,
            "fleet": fleet,
            "fleet_all": fleet_all,
            "fleet_signature": tuple(
                (rig["order_id"], rig.get("observation_id"), rig.get("provision_state"), rig["health"])
                for rig in fleet_all
            ),
            "experiment": exp,
            "experiment_signature": (
                (exp["events"][0]["id"] if exp["events"] else 0,
                 len(exp["rentals"]))
                if exp else None),
            # pool refresh must repaint Candidates even when the
            # marketplace snapshot and event stream are unchanged
            "candidate_signature": (
                ((exp.get("candidate_term_hours"),
                  ((exp.get("phase") or {}).get("value_model") or {})
                  .get("source")),
                 tuple(
                     (cv["server_id"], cv["est_its"], cv["usd_hr"],
                      cv["return_x"], cv.get("net_hr"), cv["prior"],
                      cv["fill_status"], cv.get("mode"))
                     for cv in (exp.get("candidate_values") or ())))
                if exp else None),
        }
    except (sqlite3.Error, OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise DashboardError(
            "Cannot read scanner database. Run `python3 cli.py scan --live` "
            "or select a scanner database with --db."
        ) from exc


def read_log_archive(path, order_id, pool, max_chars=200000,
                     head=False):
    """Archived miner-log tail (or head when head=True) for a rig;
    empty result when none/no table."""
    try:
        with closing(open_readonly(path)) as db:
            return fleet_store.read_log_archive(
                db, order_id, pool, max_chars, head=head)
    except (sqlite3.Error, OSError, ValueError):
        return {"text": "", "last_captured_at": None, "chunks": 0}


def read_server_details(path, server_id):
    """Return recorded listing and the newest 20 hardware/price intervals."""
    try:
        with closing(open_readonly(path)) as db:
            server = db.execute(
                "SELECT server_id, owner, country, first_seen, last_seen, latest_json "
                "FROM servers WHERE server_id=?", (server_id,)
            ).fetchone()
            if server is None:
                raise DashboardError("This server is no longer in the database.")
            specs = [dict(row) for row in db.execute(
                "SELECT observed_at, valid_to, cpu_model, cpu_offer, gpu_array_json, "
                "vram_gb, cuda_version, ram_gb, specs_json "
                "FROM server_specs WHERE server_id=? ORDER BY id DESC LIMIT 20",
                (server_id,),
            )]
            prices = [dict(row) for row in db.execute(
                "SELECT observed_at, valid_to, on_demand_usd_day, price_json "
                "FROM server_prices WHERE server_id=? ORDER BY id DESC LIMIT 20",
                (server_id,),
            )]
            spot_record = latest_quotes(db).get(server_id)
            rentals = []
            if db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table'"
                    " AND name='fleet_rentals'").fetchone():
                order_index = {
                    o["order_id"]: o for o in read_orders_history(db)}
                rows = db.execute(
                    "SELECT * FROM fleet_rentals WHERE server_id=?"
                    " ORDER BY order_id DESC", (server_id,)).fetchall()
                for row in rows:
                    rig = dict(row)
                    rig["shares_hr"] = _shares_per_hr(db, rig["order_id"])
                    rig["its_total"] = _its_per_order(db, rig["order_id"])
                    _rig_cost(rig, order_index)
                    rentals.append(rig)
        result = dict(server)
        result["listing"] = json.loads(result.pop("latest_json"))
        result["specs"] = specs
        result["prices"] = prices
        result["spot_record"] = spot_record
        result["spot"] = summarize_quote(spot_record)
        result["rentals"] = rentals
        return result
    except DashboardError:
        raise
    except (sqlite3.Error, OSError, ValueError, KeyError, TypeError) as exc:
        raise DashboardError("Cannot read server history; refresh and try again.") from exc
