"""Tests for pool_stats: extraction, recording, rental-value
attribution, the exponent fit, and the collector/experiment hooks.
The jetski wallet is a secret: only a fake one appears here, and
nothing that leaves this module may contain it."""

import io
import json
import os
import sqlite3
import sys
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import cli  # noqa: E402
import experiment  # noqa: E402
import pool_stats  # noqa: E402

PREFIX = "TESTWALLETaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
WALLET = PREFIX + ".office"

T0 = datetime(2026, 11, 15, 10, 0, 0, tzinfo=timezone.utc)


def _ts(minutes):
    return (T0 + timedelta(minutes=minutes)).isoformat()


def make_doc(ts_min=0, epoch=240, shares=1000, counted=800,
             miners=None):
    if miners is None:
        miners = [{
            "alias": "rig-a", "trainer": "trainer-x",
            "wallet": PREFIX + ".rig1",
            "sol": 100.0, "its": 200.0, "cuda_hashrate": 3.0,
            "avx512_hashrate": 1.0, "avx2_hashrate": 0.5,
            "last_seen": _ts(ts_min),
        }, {
            "alias": "rig-b", "trainer": "trainer-y",
            "wallet": PREFIX + ".rig2",
            "sol": 50.0, "its": 100.0,
            "last_seen": _ts(ts_min),
        }, {
            "alias": "stranger", "trainer": "trainer-x",
            "wallet": "OTHERWALLET.rig", "sol": 999.0, "its": 1.0,
        }]
    sub = {
        "sub_user": PREFIX, "epoch": epoch, "shares": shares,
        "solutions": 5, "counted_solutions": counted,
        "estimated_gains": counted * 0.001, "estimated_qus": 100.0,
        "invalid_shares": 2, "obsolete_shares": 1,
    }
    return {
        "PPLNSPOOL": {
            "metadata": {"timestamp": _ts(ts_min)[:19]},  # naive ISO
            "miners": miners,
            "summary": {
                "total_sol": 278340.0, "total_its": 56156.0,
                "total_miners": 2526, "epoch_progress": "16.8%",
                "average_sol": 110.19, "average_its": 22.23,
            },
        },
        "SOLOPOOL": {"miners": [
            {"alias": "solo-rig", "wallet": PREFIX + ".solo",
             "sol": 33.0},
            {"alias": "other-solo", "wallet": "OTHERWALLET.x",
             "sol": 1.0},
        ]},
        "IdleStatus": {},
        "NETWORKSTATS": {"data": {
            "qubic_price_usd": 0.000002, "itsPoolPPLNS": 50000.0}},
        "VERIFIEDSOLO": {"sub_users": [dict(
            sub, counted_solutions=42, estimated_gains=0.06)]},
        "VERIFIEDPPLNS": {"sub_users": [sub]},
        "poolPerformance": {
            "totalSolutionsPerHour": 1200.0,
            "trainers": [{"family": "trainer-x",
                          "solutionsPerHour": 600.0}]},
    }


def make_db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    pool_stats.initialize(db)
    return db


def add_snap(db, ts_min, epoch, shares, counted, workers=None,
             usd_per_counted=0.001):
    """Insert a snapshot directly; workers: {alias: (sol, its)}."""
    cur = db.execute(
        "INSERT INTO pool_snapshots(fetched_at, source_ts, epoch,"
        " shares, solutions, counted, est_gains_usd, est_qus,"
        " usd_per_counted, qubic_price_usd, pool_sol_per_hour,"
        " extra_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (_ts(ts_min), _ts(ts_min), epoch, shares, shares // 2,
         counted, counted * usd_per_counted, 1.0, usd_per_counted,
         2e-6, 100.0, "{}"))
    sid = cur.lastrowid
    for alias, (sol, its) in (workers or {}).items():
        db.execute(
            "INSERT INTO pool_worker_snapshots(snapshot_id, alias,"
            " sol, its) VALUES (?,?,?,?)", (sid, alias, sol, its))
    db.commit()
    return sid


class ExtractTests(unittest.TestCase):
    def test_wallet_with_suffix_matches_prefix(self):
        ext = pool_stats.extract(make_doc(ts_min=5), WALLET)
        self.assertTrue(ext["source_ts"].endswith("+00:00"))
        self.assertEqual(ext["source_ts"][:19], _ts(5)[:19])
        self.assertEqual(ext["epoch"], 240)
        self.assertEqual(ext["shares"], 1000)
        self.assertEqual(ext["counted"], 800)
        self.assertAlmostEqual(ext["est_gains_usd"], 0.8)
        self.assertAlmostEqual(ext["usd_per_counted"], 0.001)
        self.assertEqual(ext["invalid_shares"], 2)
        self.assertEqual(ext["obsolete_shares"], 1)
        self.assertAlmostEqual(ext["qubic_price_usd"], 0.000002)
        self.assertAlmostEqual(ext["pool_sol_per_hour"], 1200.0)
        self.assertAlmostEqual(ext["pplns_total_its"], 50000.0)
        self.assertEqual(ext["trainers"], {"trainer-x": 600.0})
        self.assertEqual([w["alias"] for w in ext["workers"]],
                         ["rig-a", "rig-b"])
        w = ext["workers"][0]
        self.assertEqual((w["sol"], w["its"], w["trainer"], w["cuda"],
                          w["avx512"], w["avx2"]),
                         (100.0, 200.0, "trainer-x", 3.0, 1.0, 0.5))
        self.assertEqual(ext["solo"]["counted"], 42)
        self.assertAlmostEqual(ext["solo"]["est_gains_usd"], 0.06)
        self.assertEqual(ext["solo_workers"],
                         [{"alias": "solo-rig", "sol": 33.0}])
        self.assertAlmostEqual(ext["pool_total_sol"], 278340.0)
        self.assertAlmostEqual(ext["pool_total_its"], 56156.0)
        self.assertEqual(ext["pool_miners"], 2526)
        self.assertEqual(ext["epoch_progress"], "16.8%")

    def test_absent_wallet(self):
        ext = pool_stats.extract(make_doc(), "MISSINGWALLET")
        for key in ("epoch", "shares", "solutions", "counted",
                    "est_gains_usd", "est_qus", "usd_per_counted",
                    "invalid_shares", "obsolete_shares"):
            self.assertIsNone(ext[key], key)
        self.assertEqual(ext["workers"], [])
        self.assertEqual(ext["solo"]["counted"], None)
        self.assertIsNotNone(ext["source_ts"])
        self.assertIsNotNone(ext["qubic_price_usd"])


class RecordTests(unittest.TestCase):
    def test_record_once_ok_then_unchanged(self):
        db = make_db()
        with mock.patch.object(pool_stats, "fetch",
                               return_value=make_doc()):
            self.assertEqual(
                pool_stats.record_once(db, WALLET, now=T0), "ok")
            self.assertEqual(
                pool_stats.record_once(db, WALLET, now=T0),
                "unchanged")
        rows = db.execute("SELECT * FROM pool_snapshots").fetchall()
        self.assertEqual(len(rows), 1)
        workers = db.execute(
            "SELECT alias, sol, its FROM pool_worker_snapshots"
            " ORDER BY alias").fetchall()
        self.assertEqual([(w["alias"], w["sol"], w["its"])
                          for w in workers],
                         [("rig-a", 100.0, 200.0),
                          ("rig-b", 50.0, 100.0)])
        extra = json.loads(rows[0]["extra_json"])
        self.assertEqual(extra["solo_counted"], 42)
        self.assertEqual(extra["solo_workers"],
                         [{"alias": "solo-rig", "sol": 33.0}])
        self.assertEqual(extra["pool_total_sol"], 278340.0)
        self.assertEqual(extra["pool_total_its"], 56156.0)
        self.assertEqual(extra["pool_miners"], 2526)
        self.assertEqual(extra["epoch_progress"], "16.8%")
        self.assertNotIn(WALLET, json.dumps(extra))
        self.assertNotIn(PREFIX, json.dumps(extra))

    def test_record_once_error_returns_type_name_only(self):
        db = make_db()
        with mock.patch.object(
                pool_stats, "fetch",
                side_effect=OSError(f"boom {WALLET}")):
            status = pool_stats.record_once(db, WALLET)
        self.assertEqual(status, "OSError")
        self.assertNotIn(WALLET, status)

    def test_maybe_record_skips_within_interval(self):
        db = make_db()
        add_snap(db, ts_min=0, epoch=240, shares=1, counted=1,
                 workers={"a": (1.0, 1.0)})
        db.execute("UPDATE pool_snapshots SET fetched_at=?",
                   (_ts(5),))
        db.commit()
        with mock.patch.object(pool_stats, "fetch") as fetch:
            status = pool_stats.maybe_record(
                db, WALLET, min_interval_s=600,
                now=T0 + timedelta(minutes=9))
        self.assertEqual(status, "skip")
        fetch.assert_not_called()
        with mock.patch.object(
                pool_stats, "fetch",
                return_value=make_doc(ts_min=6)):
            status = pool_stats.maybe_record(
                db, WALLET, min_interval_s=600,
                now=T0 + timedelta(minutes=16))
        self.assertEqual(status, "ok")

    def test_fetch_rejects_non_dict(self):
        class FakeResp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with mock.patch.object(
                pool_stats.urllib.request, "urlopen",
                return_value=FakeResp(b"[1, 2]")):
            with self.assertRaises(ValueError):
                pool_stats.fetch(url="http://unused")


class RentalValueTests(unittest.TestCase):
    """The estimate allocates the wallet's cumulative counted
    solutions to the rig's latest shares at the wallet-average
    ratio -- provisional, never a measured payout."""

    def test_cumulative_wallet_allocation(self):
        db = make_db()
        add_snap(db, 0, 240, shares=1000, counted=600,
                 workers={"a": (100.0, 200.0)})
        add_snap(db, 10, 240, shares=1200, counted=780,
                 workers={"a": (160.0, 200.0)})
        add_snap(db, 20, 240, shares=1400, counted=960,
                 workers={"a": (230.0, 200.0)})
        v = pool_stats.rental_value(db, "a")
        # latest snapshot only: 230 shares at the 960/1400 ratio
        self.assertAlmostEqual(v["pre_snapshot_shares"], 100.0)
        self.assertAlmostEqual(v["shares"], 230.0)
        self.assertAlmostEqual(v["weighted"], 230.0 * 960 / 1400)
        self.assertAlmostEqual(v["usd"], 230.0 * 960 / 1400 * 0.001)
        self.assertAlmostEqual(v["weight_per_share"], 960 / 1400)
        self.assertIsNone(v["clean_frac"])
        self.assertEqual(v["method"], "wallet_average")
        self.assertEqual(v["confidence"], "provisional")
        self.assertEqual(v["as_of"], _ts(20))

    def test_absent_in_latest_snapshot(self):
        db = make_db()
        add_snap(db, 0, 240, shares=1000, counted=600,
                 workers={"a": (100.0, 200.0)})
        add_snap(db, 10, 240, shares=1200, counted=780,
                 workers={})
        v = pool_stats.rental_value(db, "a")
        # The feed can drop ended workers; retain the last known
        # shares and apply the latest same-epoch wallet ratio.
        self.assertAlmostEqual(v["shares"], 100.0)
        self.assertAlmostEqual(v["weighted"], 100 * 780 / 1200)
        self.assertAlmostEqual(v["usd"], 100 * 780 / 1200 * 0.001)
        self.assertEqual(v["pre_snapshot_shares"], 100.0)

    def test_cross_epoch_uses_latest_by_default(self):
        db = make_db()
        add_snap(db, 0, 240, shares=1000, counted=600,
                 workers={"a": (100.0, 200.0)})
        add_snap(db, 10, 240, shares=1200, counted=780,
                 workers={"a": (160.0, 200.0)})
        add_snap(db, 20, 241, shares=400, counted=300,
                 workers={"a": (30.0, 200.0)})
        v = pool_stats.rental_value(db, "a")
        # default is the newest snapshot's epoch: 30 shares at the
        # epoch-241 ratio
        self.assertAlmostEqual(v["weighted"], 30.0 * 300 / 400)
        self.assertAlmostEqual(v["usd"], 22.5 * 0.001)
        self.assertEqual(v["as_of"], _ts(20))
        self.assertEqual(v["pre_snapshot_shares"], 30.0)
        # an explicit epoch prices that epoch at its own snapshot's
        # P, not the global latest
        v = pool_stats.rental_value(db, "a", epoch=240)
        self.assertAlmostEqual(v["weighted"], 160.0 * 780 / 1200)
        self.assertAlmostEqual(v["usd"], 104.0 * 0.001)
        self.assertEqual(v["as_of"], _ts(10))

    def test_delayed_credit_cannot_amplify_a_worker(self):
        # counted jumps +140 while wallet shares stay flat and the
        # rig only added 40: the cumulative ratio still bounds the
        # allocation at sol x wallet-average, never a per-worker
        # delta share of the jump
        db = make_db()
        add_snap(db, 0, 241, shares=200, counted=40, workers={})
        add_snap(db, 10, 241, shares=200, counted=180,
                 workers={"a": (40.0, 200.0)})
        v = pool_stats.rental_value(db, "a")
        self.assertAlmostEqual(v["weighted"], 40.0 * 180 / 200)
        self.assertAlmostEqual(v["usd"], 36.0 * 0.001)
        self.assertEqual(v["pre_snapshot_shares"], 0.0)

    def test_unknown_alias_returns_none(self):
        db = make_db()
        add_snap(db, 0, 240, shares=1000, counted=600,
                 workers={"a": (100.0, 200.0)})
        self.assertIsNone(pool_stats.rental_value(db, "ghost"))


class FitExponentTests(unittest.TestCase):
    def test_recovers_beta_from_synthetic_data(self):
        db = make_db()
        shares = counted = 0.0
        sol_a = sol_b = 0.0
        deltas = [(100.0, 100.0), (50.0, 300.0), (200.0, 50.0)]
        workers = {"fast": (sol_a, 400.0), "slow": (sol_b, 100.0)}
        add_snap(db, 0, 240, shares=0, counted=0,
                 workers=dict(workers))
        for i, (d_fast, d_slow) in enumerate(deltas, start=1):
            sol_a += d_fast
            sol_b += d_slow
            shares += d_fast + d_slow
            counted += 0.5 * (d_fast * 400.0 ** 0.3
                              + d_slow * 100.0 ** 0.3)
            add_snap(db, i * 10, 240, shares=shares, counted=counted,
                     workers={"fast": (sol_a, 400.0),
                              "slow": (sol_b, 100.0)})
        fit = pool_stats.fit_exponent(db)
        self.assertEqual(fit["n_intervals"], 3)
        self.assertAlmostEqual(fit["best_beta"], 0.3)
        self.assertAlmostEqual(fit["sse_by_beta"][0.3], 0.0, places=8)
        self.assertAlmostEqual(fit["k"], 0.5)

    def test_empty_db_is_graceful(self):
        fit = pool_stats.fit_exponent(make_db())
        self.assertEqual(fit["n_intervals"], 0)
        self.assertIsNone(fit["best_beta"])
        self.assertEqual(fit["solo_intervals"], [])

    def test_min_window_min_subsamples_and_averages_its(self):
        # 10-min snapshots; min_window_min=30 selects every third so
        # each fitted window spans 3 snapshots, and its_eff is the mean
        # of positive its across ALL in-window snapshots — endpoints
        # alone would give 400 for "fast" in window 1, not 533.33.
        db = make_db()
        k_true, beta_true = 0.5, 0.3
        fast_its = [400.0, 800.0, 0.0, 400.0, 0.0, 400.0, 400.0]
        effs = {"fast": [(400.0 + 800.0 + 400.0) / 3, 400.0],
                "slow": [100.0, 100.0]}
        sol = {"fast": 0.0, "slow": 0.0}
        shares = [0.0] * 7
        counted = [0.0] * 7
        for w in range(2):
            base = w * 3
            dc = k_true * (30.0 * effs["fast"][w] ** beta_true
                           + 15.0 * effs["slow"][w] ** beta_true)
            for j in range(base + 1, base + 4):
                sol["fast"] += 10.0
                sol["slow"] += 5.0
                shares[j] = shares[j - 1] + 45.0
                counted[j] = counted[j - 1] + (dc if j == base + 3
                                               else 0.0)
        sol_a = sol_b = 0.0
        for i in range(7):
            if i:
                sol_a += 10.0
                sol_b += 5.0
            add_snap(db, i * 10, 240, shares=shares[i],
                     counted=counted[i],
                     workers={"fast": (sol_a, fast_its[i]),
                              "slow": (sol_b, 100.0)})
        fit = pool_stats.fit_exponent(db, min_window_min=30)
        self.assertEqual(fit["n_intervals"], 2)
        self.assertAlmostEqual(fit["best_beta"], 0.3)
        self.assertAlmostEqual(fit["k"], 0.5)
        self.assertAlmostEqual(fit["sse_by_beta"][0.3], 0.0, places=8)
        # without the window constraint every adjacent pair fits
        fit_all = pool_stats.fit_exponent(db)
        self.assertEqual(fit_all["n_intervals"], 6)


class CliTests(unittest.TestCase):
    def test_record_and_show_never_print_wallet(self):
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(fd)
        try:
            with mock.patch.object(
                    pool_stats, "fetch",
                    return_value=make_doc()), \
                    mock.patch.dict(os.environ,
                                    {"JETSKI_WALLET": WALLET}):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    code = pool_stats.main(
                        ["record", "--db", path,
                         "--env-file", "/nonexistent"])
                self.assertEqual(code, 0)
                with redirect_stdout(buf):
                    code = pool_stats.main(["show", "--db", path])
                self.assertEqual(code, 0)
            blob = buf.getvalue()
            self.assertNotIn(WALLET, blob)
            self.assertNotIn(PREFIX, blob)
            self.assertIn("latest:", blob)
            self.assertIn("workers:", blob)
            self.assertIn("fit_exponent:", blob)
        finally:
            os.unlink(path)


NOW = datetime(2026, 11, 15, 12, 0, 0, tzinfo=timezone.utc)


def iso(minutes_ago):
    return (NOW - timedelta(minutes=minutes_ago)).isoformat()


def _exp_phase(db):
    db.execute(
        "INSERT INTO experiment_phases(started_at, ends_at, "
        "budget_usd, reserve_usd, config_json) VALUES (?,?,?,?,?)",
        (iso(600), iso(-2880), "100", "0.50",
         json.dumps(dict(experiment.DEFAULT_CONFIG))))
    db.commit()
    return db.execute(
        "SELECT * FROM experiment_phases ORDER BY phase_id DESC"
        " LIMIT 1").fetchone()


def _ended_rental(db, phase_id, order_id=9001, ended_min=60):
    db.execute(
        "INSERT INTO fleet_rentals(order_id, server_id, alias, pool,"
        " deadline_at, max_cost_usd, quoted_cost_usd, ssh_key_path,"
        " ssh_host, ssh_port, provision_state, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (order_id, 101, "rig-x", "jetski", iso(-300), "1.0", "1.0",
         "/tmp/key", "10.0.0.1", 22, "ready", iso(300), iso(0)))
    db.execute(
        "INSERT INTO experiment_rentals(order_id, phase_id, server_id,"
        " tier, reason, x_min, y_min, hourly_usd, creation_fee_usd,"
        " quoted_cost_usd, created_at, ended_at, role)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'slot')",
        (order_id, phase_id, 101, "other", "fill", 30, 15, "0.10",
         "0.10", "0.70", iso(300), iso(ended_min)))
    db.commit()


class RentalValueEventTests(unittest.TestCase):
    def _run(self, db, phase):
        experiment.run_cycle(
            db, ":memory:", "/nonexistent-env", phase,
            dict(experiment.DEFAULT_CONFIG), live=False, now=NOW,
            say=lambda *_a: None)

    def test_event_emitted_exactly_once(self):
        db = experiment.open_db(":memory:")
        phase = _exp_phase(db)
        _ended_rental(db, phase["phase_id"], ended_min=60)
        add_snap(db, 0, 240, shares=1000, counted=800,
                 workers={"rig-x": (100.0, 200.0)})
        # ended at NOW-60min; snapshot at NOW-45min = ended+15min
        db.execute("UPDATE pool_snapshots SET source_ts=?, "
                   "fetched_at=?", (iso(45), iso(45)))
        db.commit()
        self._run(db, phase)
        rows = db.execute(
            "SELECT * FROM experiment_events WHERE kind='rental_value'"
            ).fetchall()
        self.assertEqual(len(rows), 1)
        detail = json.loads(rows[0]["detail_json"])
        self.assertAlmostEqual(detail["shares"], 100.0)
        self.assertAlmostEqual(detail["pre_snapshot_shares"], 100.0)
        self.assertAlmostEqual(detail["weighted"], 80.0)
        self.assertAlmostEqual(detail["usd_est"], 80.0 * 0.001)
        # 4h at $0.10/h + $0.10 creation fee (past the free window)
        self.assertAlmostEqual(detail["cost_usd"], 0.50, places=3)
        self.assertAlmostEqual(detail["return_x"], 0.16, places=3)
        self.assertEqual(detail["method"], "wallet_average")
        self.assertEqual(detail["confidence"], "provisional")
        self.assertNotIn(WALLET, rows[0]["detail_json"])
        self.assertNotIn("rig-x", rows[0]["detail_json"])
        self._run(db, phase)  # idempotent
        rows = db.execute(
            "SELECT * FROM experiment_events WHERE kind='rental_value'"
            ).fetchall()
        self.assertEqual(len(rows), 1)

    def test_no_event_before_snapshot_matures(self):
        db = experiment.open_db(":memory:")
        phase = _exp_phase(db)
        _ended_rental(db, phase["phase_id"], ended_min=60)
        add_snap(db, 0, 240, shares=1000, counted=800,
                 workers={"rig-x": (100.0, 200.0)})
        # snapshot at ended+10min: inside the 15-min window
        db.execute("UPDATE pool_snapshots SET source_ts=?, "
                   "fetched_at=?", (iso(50), iso(50)))
        db.commit()
        self._run(db, phase)
        rows = db.execute(
            "SELECT * FROM experiment_events WHERE kind='rental_value'"
            ).fetchall()
        self.assertEqual(rows, [])


class ScanPoolHookTests(unittest.TestCase):
    def _args(self):
        import argparse
        return argparse.Namespace(
            snapshot=None, env_file="/nonexistent-env", db=":memory:",
            spot_limit=0, top=5, watch=False, stop_event=None)

    def test_maybe_record_called_in_live_scan(self):
        payload = {"code": 0, "servers": []}
        with mock.patch.dict(cli.os.environ,
                             {"CLORE_API_KEY": "K",
                              "JETSKI_WALLET": WALLET}), \
                mock.patch.object(cli, "fetch_marketplace",
                                  return_value=payload), \
                mock.patch.object(cli.orders, "refresh_inventory",
                                  return_value=True), \
                mock.patch.object(cli.orders, "refresh_wallets"), \
                mock.patch.object(cli.pool_stats,
                                  "maybe_record") as rec:
            with redirect_stdout(io.StringIO()):
                code = cli.scan_once(self._args())
        self.assertEqual(code, 0)
        self.assertEqual(rec.call_count, 1)
        self.assertEqual(rec.call_args[0][1], WALLET)
        self.assertEqual(rec.call_args[0][2], 600)

    def test_pool_failure_does_not_break_scan(self):
        payload = {"code": 0, "servers": []}
        with mock.patch.dict(cli.os.environ,
                             {"CLORE_API_KEY": "K",
                              "JETSKI_WALLET": WALLET}), \
                mock.patch.object(cli, "fetch_marketplace",
                                  return_value=payload), \
                mock.patch.object(cli.orders, "refresh_inventory",
                                  return_value=True), \
                mock.patch.object(cli.orders, "refresh_wallets"), \
                mock.patch.object(cli.pool_stats, "maybe_record",
                                  side_effect=RuntimeError("boom")):
            with redirect_stdout(io.StringIO()):
                code = cli.scan_once(self._args())
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
