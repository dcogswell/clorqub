"""Explicit, bounded six-wallet measurement batch; never fit payout weights."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import fcntl
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import blacklist
import experiment as exp
import fleet_store
import market
import orders
import pool_stats
import rent

ROOT = Path(__file__).resolve().parents[1]
DB = "/home/daryl/.local/share/clorqub/clorqub.sqlite3"
ENV = "/home/daryl/.config/clorqub/env"
WALLETS = Path("/home/daryl/.config/clorqub/weight-wallets.json")
CAP = Decimal("3")
TOTAL = Decimal("20")
PROFILES = {"RTX3060": ("RTX 3060", 1),
            "RTX3080": ("RTX 3080 Ti", 1),
            "RTX3080AVX2": ("RTX 3080 Ti", 1),
            "RTX3090": ("RTX 3090", 1),
            "RTX4090": ("RTX 4090", 1),
            "RTX5080": ("RTX 5080", 1),
            "MultiGPU": ("RTX 3060", 2)}
# Instruction sets that definitely lack AVX512: Xeon E5/E7 v3-v4
# (Haswell/Broadwell-EP), Ryzen 1xxx-5xxx and Threadripper 1-3xxx
# (Zen..Zen3), EPYC 7xxx (Rome/Milan), Core i-4xxx..10xxx (Comet Lake;
# 11th-gen Rocket Lake excluded), old Pentium/Celeron.
AVX2_ONLY_CPU = re.compile(
    r"(E5-\d{4}\s*v[3-4]|E7-\d{4}\s*v[3-4]|Ryzen\s+\w*\s*[1-5]\d{3}|"
    r"Threadripper\s+\w*\s*[1-3]\d{3}|EPYC\s+7\d{2}|"
    r"i[3579]-[4-9]\d{3}\b|i[3579]-10\d{3}|Celeron|G\d{4}\b)", re.I)
AVX2_PROFILES = {"RTX3080AVX2"}
# Zen3 hosts (5950X/59xx/58xx) are the closest ISA-only contrast to the
# Zen4 AVX512 3080 Ti reference rig s111997.
AVX3_PREFERRED = re.compile(r"Ryzen\s+\w*\s+5[0-9]{3}", re.I)


def now():
    return datetime.now(timezone.utc)


def stamp():
    return now().isoformat()


def write(path, value):
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as out:
        json.dump(value, out, indent=2, default=str)
        out.write("\n")
    os.replace(tmp, path)


def append(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as out:
        out.write(json.dumps(value, default=str) + "\n")


def wallets():
    if WALLETS.stat().st_mode & 0o077:
        raise ValueError("Wallet mapping must be owner-only")
    result = json.loads(WALLETS.read_text())
    if set(result) != set(PROFILES) \
            or len(set(result.values())) != len(PROFILES):
        raise ValueError("A distinct wallet is required for every profile")
    if any(not re.fullmatch("[A-Z]{60}", w) for w in result.values()):
        raise ValueError("Invalid wallet format")
    return result


def database():
    db = sqlite3.connect(DB, timeout=30)
    db.row_factory = sqlite3.Row
    return db


def phase(db):
    row = db.execute(
        "SELECT * FROM experiment_phases WHERE closed_at IS NULL "
        "ORDER BY phase_id DESC LIMIT 1").fetchone()
    if row is None or Decimal(row["budget_usd"]) != TOTAL:
        raise ValueError("Requires the existing open $20 phase")
    return row, exp.validate_config(json.loads(row["config_json"]))


def quoted(trial):
    return Decimal(trial["ctx"]["quote"]["cost_bound_usd"])


def references(directory):
    contexts, aliases = {}, {}
    if directory is None:
        return contexts, aliases, Decimal(0)
    latest = json.loads((directory / "state.json").read_text())
    prior = (Decimal(latest.get("prior_quoted_bound_usd", "0"))
             + sum((quoted(t) for t in latest["trials"]), Decimal(0)))
    seen = set()
    while directory is not None:
        resolved = directory.resolve()
        if resolved in seen:
            raise ValueError("Circular reference batch")
        seen.add(resolved)
        data = json.loads((directory / "state.json").read_text())
        # A still-running reference is safe: its trial contexts and prior
        # bound are fixed at prepare time and state.json writes are atomic.
        if data["state"] not in ("running", "stopped", "halted"):
            raise ValueError("Reference batch must be launched")
        for trial in data["trials"]:
            label = trial["label"]
            contexts.setdefault(label, trial["ctx"])
            aliases.setdefault(label, set()).add(trial["ctx"]["alias"])
        parent = data.get("reference_directory")
        directory = Path(parent) if parent else None
    return contexts, aliases, prior


def configuration(server):
    specs = server.get("specs") or {}
    return (server["id"], specs.get("cpu"), specs.get("cpus"),
            specs.get("ram"), server.get("gpu_array"),
            str(server.get("cuda_version")))


def hard_stop(manifest, managed):
    bound = exp._dt(managed["created_at"]) + timedelta(
        minutes=manifest.get("max_minutes", 20))
    if managed["deadline_at"]:
        bound = min(bound, exp._dt(managed["deadline_at"])
                    - timedelta(minutes=1))
    return bound


def budget(db, manifest):
    ph, cfg = phase(db)
    if ph["phase_id"] != manifest["phase_id"]:
        raise ValueError("Phase changed")
    if fleet_store.pending_attempts(db):
        raise ValueError("Unresolved rental attempt; no further spend")
    bound = sum((quoted(t) for t in manifest["trials"]), Decimal(0))
    if bound > Decimal(manifest["batch_cap"]):
        raise ValueError("Batch quotes exceed the approved batch cap")
    cumulative_cap = Decimal(manifest.get("cumulative_cap_usd", str(CAP)))
    if Decimal(manifest.get("prior_quoted_bound_usd", "0")) + bound \
            > cumulative_cap:
        raise ValueError("Cumulative quotes exceed approved cap")
    future = sum((quoted(t) for t in manifest["trials"]
                  if not t.get("attempted_at") and not t.get("skipped_at")),
                 Decimal(0))
    if exp.committed_usd(db, ph["phase_id"], now(), cfg) + future > TOTAL:
        raise ValueError("Batch would exceed existing $20 phase")
    return ph, cfg


def prepare(directory, reference=None, profiles=None, batch_cap=CAP,
            max_minutes=20, cumulative_cap=None, exclude_ids=()):
    wallets()
    cap_limit = cumulative_cap if cumulative_cap is not None else CAP
    if not cap_limit.is_finite() or cap_limit < CAP \
            or cap_limit > Decimal("8"):
        raise ValueError("Cumulative cap must be between $3 and $8")
    if not batch_cap.is_finite() or not Decimal(0) < batch_cap <= cap_limit:
        raise ValueError("Batch cap must be positive and within the approved cap")
    if max_minutes not in (20, 55):
        raise ValueError("Only the approved 20/55-minute protocols are supported")
    selected = profiles or list(PROFILES)
    if len(set(selected)) != len(selected) or set(selected) - set(PROFILES):
        raise ValueError("Unknown or duplicate profiles")
    previous, _, prior_bound = references(reference)
    if prior_bound > cap_limit:
        raise ValueError("Reference spend already exceeds the approved cap")
    # A label may change server only while its wallet carries at most a
    # few prior shares; contamination is recorded on the trial.
    MAX_CONTAMINATION = 5
    clean = {}
    if reference is not None:
        wmap = wallets()
        doc = pool_stats.fetch()
        for label in set(previous) & set(selected):
            stats = pool_stats.extract(doc, wmap[label])
            shares = stats["shares"] or 0
            clean[label] = (shares <= MAX_CONTAMINATION and not any(
                (w.get("sol") or 0) > MAX_CONTAMINATION
                for w in stats["workers"]))
    db = database()
    ph, cfg = phase(db)
    state = exp.build_state(db, ph, now(), cfg, apply_endings=False,
                            say=lambda *_: None)
    api, _ = rent.load_secrets(ENV, need_creds=False)
    listings = rent.marketplace(api).get("servers", [])
    excluded = state["excluded"] | state["quote_rejected"]
    excluded |= set(blacklist.active(db))
    forced = set(exclude_ids)
    excluded |= forced
    trials = []
    for label in selected:
        model, count = PROFILES[label]
        candidates = []
        for server in listings:
            gpus = server.get("gpu_array") or []
            reference_match = (reference is not None and label in previous
                               and server["id"]
                               == previous[label]["server"]["id"]
                               and server["id"] not in forced)
            if (len(gpus) != count
                    or (server["id"] in excluded and not reference_match)):
                continue
            if reference is not None and label in previous \
                    and not reference_match and not clean.get(label):
                continue
            models = [market.offered_gpu({**server, "gpu_array": [g]})
                      for g in gpus]
            day = market.price_per_day_usd(server, "on-demand")
            rel = server.get("reliability")
            if (any(g != model for g in models)
                    or not market.available(server) or not day
                    or day / 24 * 1.05 > 0.50
                    or rel is None or rel < 0.75):
                continue
            cpu = str((server.get("specs") or {}).get("cpu") or "")
            if label in AVX2_PROFILES:
                # ISA must be knowable: only allowlist-matched AVX2-only
                # CPUs, and never the same host as the AVX512 reference.
                if not AVX2_ONLY_CPU.search(cpu) or server["id"] == 111997:
                    continue
            if label == "RTX3080":
                preferred = server["id"] == 111997
            elif label in AVX2_PROFILES:
                preferred = bool(AVX3_PREFERRED.search(cpu))
            else:
                preferred = False
            candidates.append(((not preferred, "unknown" in cpu.lower(),
                                day, -rel, server["id"]), server))
        ctx = trial_override = None
        for _, server in sorted(candidates)[:8]:
            try:
                ctx = rent.quote_order(
                    server_id=server["id"], hours=1,
                    max_cost_usd=str(batch_cap),
                    pool="jetski", cpu_threads=None, db_path=DB,
                    env_file=ENV, key_path=rent.DEFAULT_KEY, auto_renew=False,
                    expected_gpu_count=count,
                    say=lambda *_: None, warn=lambda *_: None)
                if reference is not None and label in previous \
                        and configuration(ctx["server"]) != configuration(
                            previous[label]["server"]):
                    if not clean.get(label):
                        raise ValueError(
                            "Reference hardware changed during quote")
                    stats = pool_stats.extract(
                        doc, wallets()[label])
                    trial_override = {
                        "previous_server_id": previous[label]["server"]["id"],
                        "previous_gpu_array": previous[label]["server"]
                        .get("gpu_array"),
                        "contamination_shares": stats["shares"] or 0,
                        "reason": "few_prior_wallet_shares"}
                break
            except (ValueError, rent.APIError):
                ctx = None
                continue
        if ctx is None:
            raise ValueError("No eligible fresh quote for " + label)
        trial = {"label": label, "ctx": ctx, "state": "quoted"}
        if trial_override is not None:
            trial["config_override"] = trial_override
        trials.append(trial)
        print(label, "server", ctx["server_id"], "quote",
              ctx["quote"]["cost_bound_usd"], flush=True)
    manifest = {"batch": directory.name, "prepared_at": stamp(),
                "phase_id": ph["phase_id"], "batch_cap": str(batch_cap),
                "phase_cap": str(TOTAL), "state": "prepared",
                "min_pool_shares": 100,
                "max_minutes": max_minutes,
                "prior_quoted_bound_usd": str(prior_bound),
                "cumulative_cap_usd": str(cap_limit),
                "reference_directory": (str(reference.resolve())
                                        if reference else None),
                "observe_until": (state["epoch_ends_at"]
                                  + timedelta(hours=1)).isoformat(),
                "trials": trials}
    budget(db, manifest)
    directory.mkdir(mode=0o700, parents=False, exist_ok=False)
    write(directory / "state.json", manifest)
    print("Total quote bound:", sum(map(quoted, trials), Decimal(0)))
    db.close()


def observe(directory):
    mapping = wallets()
    manifest = json.loads((directory / "state.json").read_text())
    until = exp._dt(manifest["observe_until"])
    while now() < until:
        captured = stamp()
        try:
            doc = pool_stats.fetch()
            rows = {label: pool_stats.extract(doc, wallet)
                    for label, wallet in mapping.items()}
            append(directory / "pool.jsonl",
                   {"fetched_at": captured, "profiles": rows})
            write(directory / "pool-latest.json",
                  {"fetched_at": captured, "profiles": rows})
        except Exception as exc:
            append(directory / "observer-errors.jsonl",
                   {"at": captured, "error": type(exc).__name__})
        try:
            manifest = json.loads((directory / "state.json").read_text())
            ended = manifest.get("stopped_at")
            interval = (600 if ended and now() - exp._dt(ended)
                        > timedelta(hours=1) else 60 if ended else 30)
        except (ValueError, OSError):
            interval = 30
        time.sleep(interval)


def place(trial):
    previous = os.environ.get("JETSKI_WALLET")
    os.environ["JETSKI_WALLET"] = wallets()[trial["label"]]
    try:
        return rent.create_order_flow(
            trial["ctx"], say=lambda *_: None, warn=lambda *_: None)
    finally:
        if previous is None:
            os.environ.pop("JETSKI_WALLET", None)
        else:
            os.environ["JETSKI_WALLET"] = previous


def register(db, manifest, trial, result):
    rc, info = result
    trial["result"] = {"rc": rc, **exp._safe_info(info)}
    oid = info.get("order_id")
    managed = db.execute("SELECT * FROM fleet_rentals WHERE order_id=?",
                         (oid,)).fetchone() if oid else None
    if managed is None:
        trial["state"] = "failed"
        if oid:
            raise ValueError("Acknowledged order missing its managed receipt")
        return
    ctx = trial["ctx"]
    q = ctx["quote"]
    trial.update(order_id=oid, created_at=managed["created_at"],
                 hard_stop_at=hard_stop(manifest, managed).isoformat(),
                 state="mining")
    with db:
        db.execute(
            "INSERT INTO experiment_rentals "
            "(order_id,phase_id,server_id,tier,reason,x_min,y_min,"
            "hourly_usd,creation_fee_usd,quoted_cost_usd,created_at,"
            "role,burst_id) VALUES (?,?,?,'weight',?,0,0,?,?,?,?,'weight',NULL)",
            (oid, manifest["phase_id"], ctx["server_id"],
             "weight_batch:" + manifest["batch"] + ":" + trial["label"],
             q["hourly_cost_usd"], q["creation_fee_usd"],
             q["cost_bound_usd"], managed["created_at"]))
    exp.emit(db, now(), manifest["phase_id"], oid, ctx["server_id"],
             "weight_trial", {"batch": manifest["batch"],
                              "profile": trial["label"]})
    if managed["provision_state"] == "cancelled":
        finish(db, trial, "provision_cancelled")


def finish(db, trial, reason):
    trial.update(state="ended", ended_at=stamp(), stop_reason=reason)
    if reason != "accepted_share_target":
        trial["measurement_status"] = "inconclusive"
    with db:
        db.execute("UPDATE experiment_rentals SET ended_at=?,end_reason=? "
                   "WHERE order_id=? AND ended_at IS NULL",
                   (trial["ended_at"], "weight:" + reason, trial["order_id"]))


def sample(db, directory, trial, share_target):
    row = db.execute(
        "SELECT id,captured_at,status_json FROM rig_observations "
        "WHERE order_id=? ORDER BY id DESC LIMIT 1",
        (trial["order_id"],)).fetchone()
    status = json.loads(row["status_json"]) if row and row["status_json"] else {}
    age = (now() - exp._dt(row["captured_at"])).total_seconds() if row else 99999
    gpu = (status.get("its") or {}).get("gpu")
    count = len((status.get("hardware") or {}).get("gpus") or [])
    healthy = (0 <= age <= 120 and isinstance(gpu, (int, float))
               and not isinstance(gpu, bool)
               and math.isfinite(gpu) and gpu > 0
               and (status.get("miner") or {}).get("running") is True
               and status.get("pool") == "jetski"
               and status.get("alias") == trial["ctx"]["alias"]
               and count == trial["ctx"]["expected_gpu_count"])
    if healthy:
        trial.setdefault("stable_since", stamp())
    else:
        trial.pop("stable_since", None)
    if row and row["id"] != trial.get("last_observation_id"):
        trial["last_observation_id"] = row["id"]
        append(directory / "telemetry.jsonl",
               {"at": stamp(), "label": trial["label"],
                "order_id": trial["order_id"],
                "captured_at": row["captured_at"],
                "status": {k: status.get(k) for k in (
                    "alias", "hardware", "its", "miner", "package",
                    "pool", "shares", "trainer", "updated_at")}})
    # Hashing is a prerequisite, not a useful measurement by itself.
    # Use wallet-level accepted shares; worker.sol is a different counter.
    pool_shares = None
    try:
        snapshot = json.loads((directory / "pool-latest.json").read_text())
        stats = snapshot["profiles"][trial["label"]]
        source = exp._dt(stats["source_ts"])
        if (0 <= (now() - exp._dt(snapshot["fetched_at"])).total_seconds() <= 120
                and -120 <= (now() - source).total_seconds() <= 120
                and source >= exp._dt(trial["created_at"])):
            pool_shares = stats["shares"]
            trial["confirmed_pool_shares"] = pool_shares
            trial["pool_source_ts"] = stats["source_ts"]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    enough_shares = (isinstance(pool_shares, (int, float))
                     and not isinstance(pool_shares, bool)
                     and math.isfinite(pool_shares)
                     and pool_shares >= share_target)
    stable = (trial.get("stable_since") and now()
              - exp._dt(trial["stable_since"]) >= timedelta(minutes=5))
    if enough_shares and stable:
        trial["measurement_status"] = "share_sample_pending_weighted_credit"
        return "accepted_share_target"
    if now() >= exp._dt(trial["hard_stop_at"]):
        trial["measurement_status"] = "inconclusive"
        return "inconclusive_time_limit"
    return None


def run(directory):
    manifest = json.loads((directory / "state.json").read_text())
    if any(t.get("attempted_at") for t in manifest["trials"]):
        raise ValueError("Batch already attempted; no automatic paid replay")
    if exp.runner_alive(DB):
        raise ValueError("Pause profit runner first; existing rigs keep mining")
    db = database()
    budget(db, manifest)
    doc = pool_stats.fetch()
    baseline = {label: pool_stats.extract(doc, wallet)
                for label, wallet in wallets().items()}
    ref = manifest.get("reference_directory")
    previous, allowed_aliases, _ = references(Path(ref) if ref else None)
    for trial in manifest["trials"]:
        label = trial["label"]
        stats = baseline[label]
        if ref and label in previous:
            if configuration(trial["ctx"]["server"]) != configuration(
                    previous[label]["server"]):
                if not trial.get("config_override"):
                    raise ValueError(
                        "Cannot mix configurations on one wallet")
                # Still within the contamination tolerance at launch?
                if not (stats["shares"] in (None, 0)
                        or stats["shares"] <= 5) or any(
                        (w.get("sol") or 0) > 5 for w in stats["workers"]):
                    raise ValueError(
                        "Wallet gained shares; cannot change configuration")
            if any(w["alias"] not in allowed_aliases[label]
                   for w in stats["workers"]):
                raise ValueError("Unexpected mining on reference wallet: " + label)
        elif (stats["shares"] not in (None, 0)
              or stats["counted"] not in (None, 0) or stats["workers"]):
            raise ValueError("Experimental wallet already has activity: " + label)
        if (stats["source_ts"] is None or not -120 <=
                (now() - exp._dt(stats["source_ts"])).total_seconds() <= 120):
            raise ValueError("Fresh pool baseline unavailable")
        if label in previous and stats["shares"] is not None \
                and stats["shares"] >= manifest["min_pool_shares"]:
            trial.update(skipped_at=stamp(), state="skipped_existing_sample",
                         measurement_status="share_sample_pending_weighted_credit")
    write(directory / "baseline.json",
          {"fetched_at": stamp(), "profiles": baseline})
    initial = {r[0] for r in db.execute("SELECT order_id FROM fleet_rentals")}
    manifest.update(state="running", started_at=stamp(),
                    initial_order_ids=sorted(initial))
    write(directory / "state.json", manifest)
    future = pending = None
    halted = False
    with ThreadPoolExecutor(max_workers=1) as worker:
        while True:
            if future and future.done():
                try:
                    register(db, manifest, pending, future.result())
                except Exception as exc:
                    pending["error"] = type(exc).__name__
                    halted = True
                future = pending = None
                if fleet_store.pending_attempts(db):
                    halted = True
            active = [t for t in manifest["trials"] if t["state"] == "mining"]
            for trial in active:
                ended = db.execute(
                    "SELECT ended_at FROM experiment_rentals WHERE order_id=?",
                    (trial["order_id"],)).fetchone()
                if ended and ended["ended_at"]:
                    trial.update(state="ended", ended_at=ended["ended_at"],
                                 measurement_status="inconclusive")
                    continue
                try:
                    reason = sample(db, directory, trial,
                                    manifest["min_pool_shares"])
                except Exception as exc:
                    trial["sample_error"] = type(exc).__name__
                    trial.pop("stable_since", None)
                    reason = ("inconclusive_telemetry_time_limit"
                              if now() >= exp._dt(trial["hard_stop_at"])
                              else None)
                    trial["measurement_status"] = "inconclusive"
                if not reason:
                    continue
                # Existing fleet IDs are never eligible for this cancellation.
                if trial["order_id"] in initial:
                    raise ValueError("Refusing to touch a preexisting order")
                rc, _ = rent.cancel_managed_order(
                    trial["order_id"], DB, ENV,
                    say=lambda *_: None, warn=lambda *_: None)
                if rc == 0:
                    finish(db, trial, reason)
                    print("ENDED", trial["label"], trial["order_id"], reason,
                          flush=True)
                else:
                    trial["cancel_unconfirmed_at"] = stamp()
            unstarted = [t for t in manifest["trials"]
                         if not t.get("attempted_at") and not t.get("skipped_at")]
            active = [t for t in manifest["trials"] if t["state"] == "mining"]
            if not future and unstarted and not halted:
                urgent = any((exp._dt(t["hard_stop_at"]) - now())
                             .total_seconds() < rent.PROVISION_SECONDS + 90
                             for t in active)
                if not urgent:
                    try:
                        if exp.runner_alive(DB):
                            raise ValueError("Competing profit runner")
                        budget(db, manifest)
                        api, _ = rent.load_secrets(ENV, need_creds=False)
                        balance = next((Decimal(str(w["balance"])) for w in
                                        orders.sanitize_wallets(
                                            orders.fetch_wallets(api))
                                        if w["name"] == "USD-Blockchain"), None)
                        pending = unstarted[0]
                        ph, cfg = phase(db)
                        need = (exp._active_burn(db, ph["phase_id"], now())
                                + Decimal(str(ph["reserve_usd"]))
                                + quoted(pending))
                        if balance is None or balance < need:
                            raise ValueError("Insufficient guarded balance")
                        pending["attempted_at"] = stamp()
                        write(directory / "state.json", manifest)
                        future = worker.submit(place, pending)
                        print("STARTING", pending["label"],
                              pending["ctx"]["server_id"], flush=True)
                    except Exception as exc:
                        manifest["halt_error"] = type(exc).__name__
                        halted = True
            if not future and not active and (not unstarted or halted):
                break
            write(directory / "state.json", manifest)
            time.sleep(3)
    manifest.update(state="stopped" if not halted else "halted",
                    stopped_at=stamp(),
                    measurement_status=(
                        "share_samples_pending_weighted_credit"
                        if all(t.get("measurement_status") ==
                               "share_sample_pending_weighted_credit"
                               for t in manifest["trials"])
                        else "inconclusive_samples"))
    write(directory / "state.json", manifest)
    db.close()


def guard(directory):
    """Independent hard-stop watcher; never creates orders or touches old IDs."""
    while True:
        manifest = json.loads((directory / "state.json").read_text())
        if manifest.get("started_at"):
            db = database()
            initial = set(manifest["initial_order_ids"])
            owned_live = False
            for trial in manifest["trials"]:
                ctx = trial["ctx"]
                row = db.execute(
                    "SELECT f.* FROM fleet_rentals f JOIN rental_attempts a "
                    "ON a.order_id=f.order_id WHERE f.alias=? "
                    "AND f.server_id=? AND a.state='matched' "
                    "ORDER BY f.created_at DESC LIMIT 1",
                    (ctx["alias"], ctx["server_id"])).fetchone()
                if (row is None or row["order_id"] in initial
                        or row["provision_state"] in ("cancelled", "ended")):
                    continue
                owned_live = True
                if now() < hard_stop(manifest, row):
                    continue
                rc, _ = rent.cancel_managed_order(
                    row["order_id"], DB, ENV,
                    say=lambda *_: None, warn=lambda *_: None)
                append(directory / "guard.jsonl",
                       {"at": stamp(), "label": trial["label"],
                        "order_id": row["order_id"], "cancel_rc": rc})
                if rc == 0:
                    if not db.execute(
                            "SELECT 1 FROM experiment_rentals WHERE order_id=?",
                            (row["order_id"],)).fetchone():
                        register(db, manifest, trial, (1, {
                            "order_id": row["order_id"],
                            "stage": "guard_recovered"}))
                    finish(db, {**trial, "order_id": row["order_id"]},
                           "guard_time_limit")
            db.close()
            if manifest["state"] in ("stopped", "halted") and not owned_live:
                return
            if now() - exp._dt(manifest["started_at"]) > timedelta(minutes=90):
                return
        time.sleep(10)


def resume(directory):
    """Restore the original controller only after trial liabilities resolve."""
    while True:
        manifest = json.loads((directory / "state.json").read_text())
        if manifest["state"] in ("stopped", "halted"):
            break
        time.sleep(10)
    db = database()
    while True:
        blocked = ("blocked_unresolved_attempt"
                   if fleet_store.pending_attempts(db) else None)
        for trial in manifest["trials"]:
            ctx = trial["ctx"]
            # Resolve by the paid-attempt receipt as well as the manifest:
            # the controller could fail between acknowledgement and booking.
            receipt = db.execute(
                "SELECT order_id FROM rental_attempts WHERE alias=? "
                "AND server_id=? AND state='matched' "
                "ORDER BY created_at DESC LIMIT 1",
                (ctx["alias"], ctx["server_id"])).fetchone()
            oid = receipt["order_id"] if receipt else trial.get("order_id")
            if oid is not None:
                row = db.execute(
                    "SELECT ended_at FROM experiment_rentals WHERE order_id=? "
                    "AND phase_id=? AND role='weight'",
                    (oid, manifest["phase_id"])).fetchone()
                if row is None or row["ended_at"] is None:
                    blocked = "blocked_unended_trial"
        if blocked is None:
            break
        write(directory / "resume.json", {"at": stamp(), "state": blocked})
        time.sleep(15)
    ph, _ = phase(db)
    if not exp.runner_alive(DB):
        with open(Path(DB).parent / "experiment.log", "ab") as log:
            child = subprocess.Popen(
                [str(ROOT / ".venv/bin/python3"), "-u",
                 str(ROOT / "experiment.py"), "run", "--live",
                 "--confirm-budget", str(ph["budget_usd"])],
                cwd=ROOT, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                start_new_session=True)
        write(directory / "resume.json",
              {"at": stamp(), "state": "started", "pid": child.pid})
    else:
        write(directory / "resume.json",
              {"at": stamp(), "state": "already_running"})
    db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "observe", "run", "guard",
                                        "resume"))
    parser.add_argument("directory", type=Path)
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--profiles")
    parser.add_argument("--batch-cap-usd", type=Decimal, default=CAP)
    parser.add_argument("--cumulative-cap-usd", type=Decimal)
    parser.add_argument("--max-minutes", type=int, choices=(20, 55), default=20)
    parser.add_argument("--exclude-ids")
    args = parser.parse_args()
    if args.mode == "prepare":
        prepare(args.directory, reference=args.reference,
                profiles=args.profiles.split(",") if args.profiles else None,
                batch_cap=args.batch_cap_usd, max_minutes=args.max_minutes,
                cumulative_cap=args.cumulative_cap_usd,
                exclude_ids=[int(x) for x in args.exclude_ids.split(",")]
                if args.exclude_ids else ())
        return
    with open(args.directory / (args.mode + ".lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        {"observe": observe, "run": run, "guard": guard, "resume": resume}[args.mode](
            args.directory)


if __name__ == "__main__":
    main()
