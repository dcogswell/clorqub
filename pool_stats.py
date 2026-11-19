#!/usr/bin/env python3
"""Jetski pool snapshots -> per-rental payout-weighted value.

Pools the public pools_latest.json feed into a local SQLite database:
one wallet-level snapshot per source timestamp plus the per-worker
rows belonging to our jetski wallet. Rental values are explicitly
provisional wallet-average allocations, not measured worker payouts.
The exploratory exponent fit is diagnostic only: counted solutions
and raw shares do not update synchronously.

The wallet is a secret: it is used only to select rows and is never
stored, printed, or placed in any event.
"""

import argparse
import json
import math
import os
import sqlite3
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

from market import load_env_file


POOL_STATS_URL = "https://qubic.jetskipool.ai/pools_latest.json"

DEFAULT_DB = "~/.local/share/clorqub/clorqub.sqlite3"
DEFAULT_ENV = "~/.config/clorqub/env"

SCHEMA = """
CREATE TABLE IF NOT EXISTS pool_snapshots (
    id INTEGER PRIMARY KEY,
    fetched_at TEXT NOT NULL,
    source_ts TEXT NOT NULL UNIQUE,
    epoch INTEGER,
    shares INTEGER,
    solutions INTEGER,
    counted INTEGER,
    est_gains_usd REAL,
    est_qus REAL,
    usd_per_counted REAL,
    qubic_price_usd REAL,
    pool_sol_per_hour REAL,
    extra_json TEXT
);
CREATE TABLE IF NOT EXISTS pool_worker_snapshots (
    snapshot_id INTEGER NOT NULL REFERENCES pool_snapshots(id),
    alias TEXT NOT NULL,
    sol REAL,
    its REAL,
    trainer TEXT,
    cuda REAL,
    avx512 REAL,
    avx2 REAL,
    last_seen TEXT,
    PRIMARY KEY (snapshot_id, alias)
);
"""


def _iso(ts):
    return ts.astimezone(timezone.utc).isoformat(timespec="seconds")


def _num(value, cast=float):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = cast(value)
        return number if math.isfinite(number) and number >= 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def _source_ts(doc):
    """PPLNSPOOL metadata timestamp is naive ISO; treat it as UTC."""
    raw = ((doc.get("PPLNSPOOL") or {}).get("metadata") or {}).get(
        "timestamp")
    if not raw:
        return None
    ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return _iso(ts)


def fetch(timeout=30, url=POOL_STATS_URL):
    request = urllib.request.Request(
        url, headers={"User-Agent": "clorqub/0.1"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        doc = json.load(response)
    if not isinstance(doc, dict):
        raise ValueError("pool stats response is not a JSON object")
    return doc


def extract(doc, wallet):
    """Wallet-scoped view of the feed; the wallet itself is dropped."""
    prefix = wallet.split(".")[0]
    pplns = doc.get("PPLNSPOOL") or {}
    net = (doc.get("NETWORKSTATS") or {}).get("data") or {}
    perf = doc.get("poolPerformance") or {}

    sub = None
    for row in (doc.get("VERIFIEDPPLNS") or {}).get("sub_users") or []:
        if row.get("sub_user") == prefix:
            sub = row
            break
    counted = _num(sub.get("counted_solutions"), int) if sub else None
    gains = _num(sub.get("estimated_gains")) if sub else None
    solo_sub = None
    for row in (doc.get("VERIFIEDSOLO") or {}).get("sub_users") or []:
        if row.get("sub_user") == prefix:
            solo_sub = row
            break
    solo = {
        "counted": (_num(solo_sub.get("counted_solutions"), int)
                    if solo_sub else None),
        "solutions": (_num(solo_sub.get("solutions"), int)
                      if solo_sub else None),
        "est_gains_usd": (_num(solo_sub.get("estimated_gains"))
                          if solo_sub else None),
        "est_qus": (_num(solo_sub.get("estimated_qus"))
                    if solo_sub else None),
    }
    solo_workers = [{
        "alias": m.get("alias"),
        "sol": _num(m.get("sol")),
    } for m in (doc.get("SOLOPOOL") or {}).get("miners") or []
        if str(m.get("wallet") or "").split(".")[0] == prefix]
    workers = [{
        "alias": m.get("alias"),
        "sol": _num(m.get("sol")),
        "its": _num(m.get("its")),
        "trainer": m.get("trainer"),
        "cuda": _num(m.get("cuda_hashrate")),
        "avx512": _num(m.get("avx512_hashrate")),
        "avx2": _num(m.get("avx2_hashrate")),
        "last_seen": m.get("last_seen"),
    } for m in pplns.get("miners") or []
       if str(m.get("wallet") or "").split(".")[0] == prefix]
    return {
        "source_ts": _source_ts(doc),
        "epoch": _num(sub.get("epoch"), int) if sub else None,
        "shares": _num(sub.get("shares"), int) if sub else None,
        "solutions": _num(sub.get("solutions"), int) if sub else None,
        "counted": counted,
        "est_gains_usd": gains,
        "est_qus": _num(sub.get("estimated_qus")) if sub else None,
        "invalid_shares": (_num(sub.get("invalid_shares"), int)
                           if sub else None),
        "obsolete_shares": (_num(sub.get("obsolete_shares"), int)
                            if sub else None),
        "usd_per_counted": (gains / counted
                            if gains is not None and counted else None),
        "qubic_price_usd": _num(net.get("qubic_price_usd")),
        "pool_sol_per_hour": _num(perf.get("totalSolutionsPerHour")),
        "trainers": {t.get("family"): _num(t.get("solutionsPerHour"))
                     for t in perf.get("trainers") or []
                     if t.get("family")},
        "pplns_total_its": _num(net.get("itsPoolPPLNS")),
        "workers": workers,
        "solo": solo,
        "solo_workers": solo_workers,
    }


def initialize(db):
    db.executescript(SCHEMA)


def _load_wallet(env_file):
    wallet = os.environ.get("JETSKI_WALLET")
    if not wallet:
        try:
            wallet = load_env_file(
                os.path.expanduser(env_file)).get("JETSKI_WALLET")
        except OSError:
            wallet = None
    return wallet


def record_once(db, wallet, now=None, url=POOL_STATS_URL):
    """Fetch once and store; 'ok', 'unchanged' (same source_ts), or
    the exception type name (never its text -- could carry the wallet)."""
    try:
        doc = fetch(url=url)
        ext = extract(doc, wallet)
        if ext["source_ts"] is None:
            return "ValueError"
        fetched_at = _iso(now or datetime.now(timezone.utc))
        initialize(db)
        with db:
            cur = db.execute(
                "INSERT OR IGNORE INTO pool_snapshots"
                "(fetched_at, source_ts, epoch, shares, solutions,"
                " counted, est_gains_usd, est_qus, usd_per_counted,"
                " qubic_price_usd, pool_sol_per_hour, extra_json)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (fetched_at, ext["source_ts"], ext["epoch"],
                 ext["shares"], ext["solutions"], ext["counted"],
                 ext["est_gains_usd"], ext["est_qus"],
                 ext["usd_per_counted"], ext["qubic_price_usd"],
                 ext["pool_sol_per_hour"],
                 json.dumps({"trainers": ext["trainers"],
                             "pplns_total_its": ext["pplns_total_its"],
                             "invalid_shares": ext["invalid_shares"],
                             "obsolete_shares":
                                 ext["obsolete_shares"],
                             "solo_counted": ext["solo"]["counted"],
                             "solo_solutions":
                                 ext["solo"]["solutions"],
                             "solo_est_gains_usd":
                                 ext["solo"]["est_gains_usd"],
                             "solo_est_qus": ext["solo"]["est_qus"],
                             "solo_workers": ext["solo_workers"]},
                            sort_keys=True)))
            if cur.rowcount == 0:
                return "unchanged"
            snap_id = db.execute(
                "SELECT id FROM pool_snapshots WHERE source_ts = ?",
                (ext["source_ts"],)).fetchone()[0]
            db.executemany(
                "INSERT OR IGNORE INTO pool_worker_snapshots"
                "(snapshot_id, alias, sol, its, trainer, cuda, avx512,"
                " avx2, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(snap_id, w["alias"], w["sol"], w["its"], w["trainer"],
                  w["cuda"], w["avx512"], w["avx2"], w["last_seen"])
                 for w in ext["workers"] if w["alias"]])
    except Exception as exc:
        return type(exc).__name__
    return "ok"


def maybe_record(db, wallet, min_interval_s=600, now=None,
                 url=POOL_STATS_URL):
    """Record at most one snapshot per min_interval_s of wall clock."""
    try:
        initialize(db)
        newest = db.execute(
            "SELECT MAX(fetched_at) FROM pool_snapshots"
            ).fetchone()[0]
    except Exception as exc:
        return type(exc).__name__
    if newest is not None:
        now = now or datetime.now(timezone.utc)
        age = (now - datetime.fromisoformat(newest)).total_seconds()
        if age < min_interval_s:
            return "skip"
    return record_once(db, wallet, now=now, url=url)


def _snapshots(db, epoch=None):
    """Snapshots ordered by source_ts with their per-alias worker maps."""
    if epoch is not None:
        rows = db.execute(
            "SELECT * FROM pool_snapshots WHERE epoch = ?"
            " ORDER BY source_ts", (epoch,)).fetchall()
    else:
        rows = db.execute(
            "SELECT * FROM pool_snapshots ORDER BY source_ts").fetchall()
    out = []
    for row in rows:
        workers = {
            w["alias"]: dict(w) for w in db.execute(
                "SELECT * FROM pool_worker_snapshots WHERE snapshot_id=?",
                (row["id"],)).fetchall()}
        out.append({"row": row, "workers": workers})
    return out


def rental_value(db, alias, epoch=None):
    """Wallet-average allocation, not measured per-worker earnings.

    Counted solutions can update without corresponding new shares.
    Allocating interval deltas would assign delayed/reweighted earnings
    from historical miners to whichever worker happens to be online.
    Use one cumulative, same-epoch ratio and label the estimate instead.
    """
    snaps = _snapshots(db, epoch)
    if not snaps:
        return None
    latest = snaps[-1]["row"]
    snaps = [s for s in snaps if s["row"]["epoch"] == latest["epoch"]]
    worker = next((s["workers"][alias] for s in reversed(snaps)
                   if alias in s["workers"]), None)
    if worker is None or latest["epoch"] is None:
        return None
    shares = worker["sol"]
    wallet_shares = latest["shares"]
    counted = latest["counted"]
    if shares is None or not wallet_shares or counted is None \
            or not 0 <= shares <= wallet_shares or counted < 0:
        return None
    rate = counted / wallet_shares
    weighted = shares * rate
    usd_per_counted = latest["usd_per_counted"]
    first = snaps[0]["workers"].get(alias)
    return {
        "shares": shares,
        "weighted": weighted,
        "usd": (weighted * usd_per_counted
                if usd_per_counted is not None else None),
        "weight_per_share": (weighted / shares if shares else None),
        "pre_snapshot_shares": (first["sol"] or 0) if first else 0,
        "clean_frac": None,
        "intervals": 0,
        "method": "wallet_average",
        "confidence": "provisional",
        "epoch": latest["epoch"],
        "as_of": latest["source_ts"],
    }


def fit_exponent(db, epoch=None, betas=None, min_window_min=0):
    """Exploratory only: d_counted ~ k * sum(d_sol_i * its_i**beta).

    Asynchronous payout updates and unchanged worker mixes can produce
    spurious fits. This function is never used to authorize spending.

    over consecutive same-epoch snapshot pairs. With min_window_min,
    subsample each epoch's snapshots so chosen pairs are at least that
    far apart; a worker's its_eff is then the mean of its positive
    it/s over every snapshot inside the window, not just endpoints."""
    if betas is None:
        betas = [i / 20 for i in range(21)]
    snaps = _snapshots(db, epoch)
    # consecutive runs of same-epoch snapshots, ordered by source_ts
    groups = []
    for s in snaps:
        if not groups \
                or groups[-1][-1]["row"]["epoch"] != s["row"]["epoch"]:
            groups.append([])
        groups[-1].append(s)
    min_gap = timedelta(minutes=min_window_min)
    intervals = []
    for group in groups:
        chosen = [0]
        for i in range(1, len(group)):
            if (datetime.fromisoformat(group[i]["row"]["source_ts"])
                    - datetime.fromisoformat(
                        group[chosen[-1]]["row"]["source_ts"])) \
                    >= min_gap:
                chosen.append(i)
        for ci in range(len(chosen) - 1):
            i, j = chosen[ci], chosen[ci + 1]
            a, b = group[i], group[j]
            ra, rb = a["row"], b["row"]
            d_shares = (rb["shares"] or 0) - (ra["shares"] or 0)
            if d_shares <= 0:
                continue
            d_counted = (rb["counted"] or 0) - (ra["counted"] or 0)
            workers = []
            for alias, wb in b["workers"].items():
                wa = a["workers"].get(alias)
                d_sol = (wb["sol"] or 0) - ((wa or {}).get("sol") or 0)
                if d_sol <= 0:
                    continue
                its_vals = [
                    w["its"] for s in group[i:j + 1]
                    for w in (s["workers"].get(alias),)
                    if w is not None and (w["its"] or 0) > 0]
                if not its_vals:
                    continue
                workers.append((alias, d_sol,
                                sum(its_vals) / len(its_vals)))
            intervals.append({"source_ts": rb["source_ts"],
                              "d_shares": d_shares,
                              "d_counted": d_counted,
                              "workers": workers})
    solo_intervals = []
    sse_by_beta = {}
    for beta in betas:
        num = den = sse = 0.0
        for iv in intervals:
            s = sum(d_sol * its ** beta
                    for _, d_sol, its in iv["workers"])
            if s <= 0:
                continue
            num += iv["d_counted"] * s
            den += s * s
        k = num / den if den > 0 else None
        sse_by_beta[beta] = {"k": k, "sse": None}
        if k is None:
            continue
        for iv in intervals:
            s = sum(d_sol * its ** beta
                    for _, d_sol, its in iv["workers"])
            if s <= 0:
                continue
            sse += (iv["d_counted"] - k * s) ** 2
        sse_by_beta[beta]["sse"] = sse
    fitted = {b: v for b, v in sse_by_beta.items() if v["sse"] is not None}
    best_beta = (min(fitted, key=lambda b: fitted[b]["sse"])
                 if fitted else None)
    best_k = fitted[best_beta]["k"] if best_beta is not None else None
    for iv in intervals:
        big = [(alias, its, d_sol) for alias, d_sol, its in iv["workers"]
               if d_sol >= 0.9 * iv["d_shares"]]
        for alias, its, d_sol in big:
            solo_intervals.append(
                (iv["source_ts"], alias, its, d_sol,
                 (iv["d_counted"] / iv["d_shares"])
                 if iv["d_shares"] else None))
    return {"best_beta": best_beta,
            "k": best_k,
            "sse_by_beta": {b: v["sse"] for b, v in sse_by_beta.items()},
            "k_by_beta": {b: v["k"] for b, v in sse_by_beta.items()},
            "n_intervals": len(intervals),
            "solo_intervals": solo_intervals}


def _fmt(v, fmt="{:.4f}"):
    return fmt.format(v) if v is not None else "-"


def _cmd_record(db, wallet):
    status = record_once(db, wallet)
    if status not in ("ok", "unchanged"):
        print(f"record: {status}")
        return 1
    row = db.execute(
        "SELECT * FROM pool_snapshots ORDER BY id DESC LIMIT 1"
        ).fetchone()
    workers = db.execute(
        "SELECT COUNT(*) FROM pool_worker_snapshots WHERE snapshot_id=?",
        (row["id"],)).fetchone()[0]
    print(f"record: {status} source_ts={row['source_ts']} "
          f"epoch={row['epoch']} shares={row['shares']} "
          f"counted={row['counted']} "
          f"usd_per_counted={_fmt(row['usd_per_counted'], '{:.6f}')} "
          f"workers={workers}")
    return 0


def _cmd_show(db):
    rows = db.execute(
        "SELECT * FROM pool_snapshots ORDER BY source_ts").fetchall()
    if not rows:
        print("no pool snapshots")
        return 1
    last = rows[-1]
    extra = json.loads(last["extra_json"] or "{}")
    solo_gains = extra.get("solo_est_gains_usd")
    total_gains = ((last["est_gains_usd"] or 0) + (solo_gains or 0)
                   if last["est_gains_usd"] is not None
                   or solo_gains is not None else None)
    print(f"latest: source_ts={last['source_ts']} epoch={last['epoch']} "
          f"shares={last['shares']} solutions={last['solutions']} "
          f"counted={last['counted']} "
          f"est_gains=${_fmt(last['est_gains_usd'], '{:.2f}')} "
          f"solo_gains=${_fmt(solo_gains, '{:.2f}')} "
          f"total=${_fmt(total_gains, '{:.2f}')} "
          f"usd_per_counted={_fmt(last['usd_per_counted'], '{:.6f}')} "
          f"qubic=${_fmt(last['qubic_price_usd'], '{:.3g}')} "
          f"pool_sol/h={_fmt(last['pool_sol_per_hour'], '{:.0f}')}")
    print("recent deltas:")
    for a, b in list(zip(rows, rows[1:]))[-10:]:
        d_shares = (b["shares"] or 0) - (a["shares"] or 0)
        d_counted = (b["counted"] or 0) - (a["counted"] or 0)
        wps = d_counted / d_shares if d_shares > 0 else None
        print(f"  {a['source_ts']} -> {b['source_ts']}: "
              f"d_shares={d_shares} d_counted={d_counted} "
              f"weight/share={_fmt(wps)}")
    workers = db.execute(
        "SELECT * FROM pool_worker_snapshots WHERE snapshot_id=?"
        " ORDER BY sol DESC", (last["id"],)).fetchall()
    prev = rows[-2] if len(rows) >= 2 else None
    prev_w = {}
    if prev is not None:
        prev_w = {w["alias"]: dict(w) for w in db.execute(
            "SELECT * FROM pool_worker_snapshots WHERE snapshot_id=?",
            (prev["id"],)).fetchall()}
    print("workers:")
    for w in workers:
        d_sol = ((w["sol"] or 0)
                 - ((prev_w.get(w["alias"]) or {}).get("sol") or 0)
                 if prev is not None else None)
        print(f"  {w['alias']}: sol={_fmt(w['sol'], '{:.0f}')} "
              f"its={_fmt(w['its'], '{:.1f}')} "
              f"trainer={w['trainer'] or '-'}"
              + (f" d_sol={_fmt(d_sol, '{:.0f}')}"
                 if d_sol is not None else ""))
    fit = fit_exponent(db)
    print(f"fit_exponent: diagnostic_only best_beta={fit['best_beta']} "
          f"k={_fmt(fit['k'], '{:.4g}')} "
          f"intervals={fit['n_intervals']} "
          f"solo={len(fit['solo_intervals'])}")
    for ts, alias, its, d_sol, wps in fit["solo_intervals"]:
        print(f"  solo {ts}: {alias} its={its:.1f} d_sol={d_sol:.0f} "
              f"weight/share={_fmt(wps)}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Record and inspect jetski pool snapshots.")
    parser.add_argument("command", choices=["record", "show"])
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--env-file", default=DEFAULT_ENV)
    args = parser.parse_args(argv)
    db = sqlite3.connect(os.path.expanduser(args.db))
    db.row_factory = sqlite3.Row
    initialize(db)
    try:
        if args.command == "record":
            wallet = _load_wallet(args.env_file)
            if not wallet:
                print("record: no-wallet")
                return 1
            return _cmd_record(db, wallet)
        return _cmd_show(db)
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
