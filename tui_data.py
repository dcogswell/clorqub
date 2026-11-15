"""Read-only SQLite views for the TUI; no credentials or remote APIs."""

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import experiment
import fleet_store
from market import available, cuda_capable, score_listing
from orders import latest_wallets, read_inventory
from spot import latest_quotes, summarize_quote
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
            for rig in fleet_all:
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
            try:
                exp = experiment.summary(
                    db, datetime.now(timezone.utc), db_path=path)
            except Exception:
                exp = None
        listings = json.loads(latest["raw_json"])["servers"] if latest else []
        rows = []
        cuda_hidden = 0
        for listing in listings:
            if not cuda_capable(listing):
                cuda_hidden += 1
                continue
            row = score_listing(listing)
            row["listing"] = listing
            row["available"] = available(listing)
            row["spot"] = summarize_quote(quotes.get(row["server_id"]))
            rows.append(row)
        return {
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
        }
    except (sqlite3.Error, OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise DashboardError(
            "Cannot read scanner database. Run `python3 cli.py scan --live` "
            "or select a scanner database with --db."
        ) from exc


def read_log_archive(path, order_id, pool, max_chars=200000):
    """Archived miner-log tail for a rig; empty result when none/no table."""
    try:
        with closing(open_readonly(path)) as db:
            return fleet_store.read_log_archive(db, order_id, pool, max_chars)
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
