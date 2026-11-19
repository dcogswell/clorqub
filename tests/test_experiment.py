"""Tests for experiment.py (auto-rental runner), trees.py and
rig_ssh.restart_miner. All external effects are stubbed: no Clore API
calls, no SSH, dry-run by default."""

import json
import os
import sqlite3
from decimal import Decimal
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import blacklist  # noqa: E402
import experiment  # noqa: E402
import fleet_store  # noqa: E402
import orders  # noqa: E402
import pool_stats  # noqa: E402
import rig_ssh  # noqa: E402
import trees  # noqa: E402

NOW = datetime(2026, 11, 15, 12, 0, 0, tzinfo=timezone.utc)
CFG = dict(experiment.DEFAULT_CONFIG)
# the assertions below were tuned for the old default; pin it so the
# arithmetic stays stable while production uses a different prior
CFG["its_exponent"] = 0.5


def iso(minutes_ago):
    return (NOW - timedelta(minutes=minutes_ago)).isoformat()


def listing(server_id=1, cpu="Intel Core i9-10980XE",
            gpu="RTX 3070", price=2.4, reliability=0.95):
    return {
        "id": server_id, "owner": 42, "rented": False,
        "cuda_version": "13.0", "gpu_array": [gpu],
        "reliability": reliability,
        "price": {"usd": {"on_demand_usd": price, "spot": 0}},
        "specs": {"cpu": cpu, "cpus": "18/36", "gpuram": 8, "ram": 64,
                  "net": {"cc": "CA"}},
    }


def status(submitted=None, cpu=10, gpu=80, gpu_count=1, total=None):
    return {
        "its": {"cpu": cpu, "gpu": gpu,
                "total": total if total is not None
                else (cpu or 0) + (gpu or 0)},
        "shares": {"submitted": submitted},
        "hardware": {"gpus": [f"GPU {i}" for i in range(gpu_count)]
                     if gpu_count else []},
    }


def make_db():
    return experiment.open_db(":memory:")


def add_phase(db, ends_min=-2880, budget="100", reserve="0.50",
              cfg=None):
    db.execute(
        "INSERT INTO experiment_phases(started_at, ends_at, "
        "budget_usd, reserve_usd, config_json) VALUES (?,?,?,?,?)",
        (iso(60), (NOW - timedelta(minutes=ends_min)).isoformat(),
         budget, reserve, json.dumps(cfg or CFG)))
    db.commit()
    return db.execute(
        "SELECT * FROM experiment_phases ORDER BY phase_id DESC "
        "LIMIT 1").fetchone()


def add_snapshot(db, servers, minutes_ago=1):
    db.execute(
        "INSERT INTO marketplace_snapshots(captured_at, source, "
        "server_count, raw_json) VALUES (?,?,?,?)",
        (iso(minutes_ago), "api", len(servers),
         json.dumps({"servers": servers})))
    db.commit()


def add_fleet(db, order_id, server_id, state="ready",
              deadline_min=-360):
    db.execute(
        "INSERT INTO fleet_rentals(order_id, server_id, alias, pool,"
        " deadline_at, max_cost_usd, quoted_cost_usd, ssh_key_path,"
        " ssh_host, ssh_port, provision_state, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (order_id, server_id, f"rig-x-{order_id}", "jetski",
         (NOW - timedelta(minutes=deadline_min)).isoformat()
         if deadline_min is not None else None,
         "1.0", "1.0", "/tmp/key", "10.0.0.1", 22, state,
         iso(30), iso(0)))
    db.commit()


def add_rental(db, phase_id, order_id, server_id, created_min,
               x_min=30, y_min=15, tier="other", reason="fill",
               role="slot", burst_id=None, hourly="0.10"):
    db.execute(
        "INSERT INTO experiment_rentals(order_id, phase_id, server_id,"
        " tier, reason, x_min, y_min, hourly_usd, creation_fee_usd,"
        " quoted_cost_usd, created_at, role, burst_id)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (order_id, phase_id, server_id, tier, reason, x_min, y_min,
         hourly, "0.10", "0.70", iso(created_min), role, burst_id))
    db.commit()


def add_burst(db, phase_id, started_min, trigger="manual", n_target=3,
              trigger_order_id=None, judged=False):
    cur = db.execute(
        "INSERT INTO experiment_bursts(phase_id, started_at, trigger,"
        " trigger_order_id, n_target, judged_at)"
        " VALUES (?,?,?,?,?,?)",
        (phase_id, iso(started_min), trigger, trigger_order_id,
         n_target, iso(0) if judged else None))
    db.commit()
    return cur.lastrowid


def add_obs(db, order_id, minutes_ago, st):
    db.execute(
        "INSERT INTO rig_observations(order_id, captured_at,"
        " status_json) VALUES (?,?,?)",
        (order_id, iso(minutes_ago),
         json.dumps(st) if st is not None else None))
    db.commit()


def add_inventory(db, order_ids, minutes_ago=0):
    """A fresh good my_orders snapshot reporting `order_ids` live."""
    db.executescript(orders.SCHEMA)
    db.execute(
        "INSERT INTO order_inventory(captured_at, ok, orders_json)"
        " VALUES (?,?,?)",
        (iso(minutes_ago), 1,
         json.dumps([{"order_id": oid} for oid in order_ids])))
    db.commit()


def add_history(db, server_id, cpu="Intel Core i9-10980XE",
                gpu="RTX 3070", fleet_order=998, its_cpu=200,
                its_gpu=800):
    """A past fleet rental with observations: feeds cpu/gpu priors,
    coverage and the server_prior used by est_its()."""
    doc = listing(server_id=server_id, cpu=cpu, gpu=gpu)
    db.execute(
        "INSERT INTO servers(server_id, owner, country,"
        " first_seen, last_seen, latest_json)"
        " VALUES (?,?,?,?,?,?)",
        (server_id, 1, "CA", iso(500), iso(0), json.dumps(doc)))
    add_fleet(db, fleet_order, server_id)
    add_obs(db, fleet_order, 300,
            status(submitted=1, cpu=its_cpu, gpu=its_gpu))
    add_obs(db, fleet_order, 240,
            status(submitted=5, cpu=its_cpu, gpu=its_gpu))
    db.commit()
    return doc


def add_pool_snap(db, ts_min, epoch=240, shares=1000, counted=800,
                  usd_per_counted=0.002, workers=None, extra=None):
    """Insert a pool snapshot directly (ts_min = minutes ago);
    workers: {alias: (sol, its)}; extra -> extra_json."""
    cur = db.execute(
        "INSERT INTO pool_snapshots(fetched_at, source_ts, epoch,"
        " shares, solutions, counted, est_gains_usd, est_qus,"
        " usd_per_counted, qubic_price_usd, pool_sol_per_hour,"
        " extra_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (iso(ts_min), iso(ts_min), epoch, shares, shares // 2,
         counted, (counted * usd_per_counted
                   if counted is not None and usd_per_counted is not None
                   else None), 1.0, usd_per_counted,
         2e-6, 100.0, json.dumps(extra or {})))
    sid = cur.lastrowid
    for alias, (sol, its) in (workers or {}).items():
        db.execute(
            "INSERT INTO pool_worker_snapshots(snapshot_id, alias,"
            " sol, its) VALUES (?,?,?,?)", (sid, alias, sol, its))
    db.commit()
    return sid


def add_tree_report(db, source, tree_id, minutes_ago, its=50,
                    shares=0, epoch=240, seed="S"):
    """A scout agent report: which tree the pool handed an external
    miner (home PC) -- the offering a new rental would draw now."""
    db.execute(
        "INSERT INTO tree_reports(ts, source, epoch, tree_id, seed,"
        " its, shares, detail_json) VALUES (?,?,?,?,?,?,?,?)",
        (iso(minutes_ago), source, epoch, tree_id, seed, its, shares,
         "{}"))
    db.commit()


def add_event(db, phase_id, minutes_ago, kind, order_id=None,
              server_id=None, detail=None):
    db.execute(
        "INSERT INTO experiment_events(ts, phase_id, order_id,"
        " server_id, kind, detail_json) VALUES (?,?,?,?,?,?)",
        (iso(minutes_ago), phase_id, order_id, server_id, kind,
         json.dumps(detail or {})))
    db.commit()


def actions(db, phase, cfg=None, say=None):
    state = experiment.build_state(db, phase, NOW, cfg or CFG,
                                   say=lambda *_a: None)
    return experiment.decide(NOW, state, cfg or CFG, say=say), state


def kinds(acts, kind):
    return [a for a in acts if a["type"] == kind]


class DecideTests(unittest.TestCase):
    def test_startup_grace_then_dry_cancel_no_restarts(self):
        """No restarts ever: a rig inside startup_min with no share is
        left alone; past dry_after_min it is cancelled dry."""
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [])
            add_fleet(db, 1, 11)
            add_rental(db, phase["phase_id"], 1, 11, 8)
            for t in (6, 4, 2, 0.5):
                add_obs(db, 1, t, status(submitted=0))
            acts, _ = actions(db, phase)
            # inside startup grace: hashing, no shares -> no action
            self.assertEqual(kinds(acts, "cancel"), [])
            self.assertEqual(kinds(acts, "restart"), [])
            db2 = make_db()
            try:
                phase2 = add_phase(db2)
                add_snapshot(db2, [])
                add_fleet(db2, 1, 11)
                add_rental(db2, phase2["phase_id"], 1, 11, 20)
                for t in (16, 10, 2, 0.5):
                    add_obs(db2, 1, t, status(submitted=0))
                acts, _ = actions(db2, phase2)
                cancels = kinds(acts, "cancel")
                self.assertEqual(len(cancels), 1)
                self.assertEqual(cancels[0]["reason"], "dry")
                self.assertEqual(kinds(acts, "restart"), [])
            finally:
                db2.close()
        finally:
            db.close()

    def test_fault_cancels_inside_free_window_only(self):
        for label, obs_status, expect in (
                ("zero_its", status(cpu=0, gpu=0, gpu_count=1),
                 "fault:zero_its"),
                ("gpu_its", status(cpu=10, gpu=0, gpu_count=1),
                 "fault:gpu_its"),
                ("gpu_count", status(cpu=10, gpu=80, gpu_count=0),
                 "fault:gpu_count")):
            db = make_db()
            try:
                phase = add_phase(db)
                add_snapshot(db, [listing(server_id=11)])
                add_fleet(db, 1, 11)
                add_rental(db, phase["phase_id"], 1, 11, 7)
                add_obs(db, 1, 1.5, obs_status)
                add_obs(db, 1, 0.5, obs_status)
                acts, _ = actions(db, phase)
                cancels = kinds(acts, "cancel")
                self.assertEqual(len(cancels), 1, label)
                self.assertEqual(cancels[0]["reason"], expect)
                self.assertIsNone(cancels[0]["rerent_at"])
            finally:
                db.close()
        # after the free window the same fault does not cancel
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [listing(server_id=11)])
            add_fleet(db, 1, 11)
            add_rental(db, phase["phase_id"], 1, 11, 12)
            add_obs(db, 1, 1.5, status(cpu=0, gpu=0, gpu_count=1))
            add_obs(db, 1, 0.5, status(cpu=0, gpu=0, gpu_count=1))
            acts, _ = actions(db, phase)
            self.assertEqual(kinds(acts, "cancel"), [])
        finally:
            db.close()

    def test_unreachable_cancel(self):
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [listing(server_id=11)])
            add_fleet(db, 1, 11)
            add_rental(db, phase["phase_id"], 1, 11, 16)
            # collector alive, SSH failing: error rows keep arriving
            for t in (10, 5, 0.5):
                add_obs(db, 1, t, None)
            acts, _ = actions(db, phase)
            cancels = kinds(acts, "cancel")
            self.assertEqual(len(cancels), 1)
            self.assertEqual(cancels[0]["reason"], "unreachable")
            self.assertIsNone(cancels[0]["rerent_at"])
        finally:
            db.close()

    def test_stale_collector_never_cancels(self):
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [listing(server_id=11)])
            add_fleet(db, 1, 11)
            add_rental(db, phase["phase_id"], 1, 11, 30)
            # collector stopped 20 min ago: one stale error row
            add_obs(db, 1, 20, None)
            acts, _ = actions(db, phase)
            self.assertEqual(kinds(acts, "cancel"), [])
            self.assertEqual(kinds(acts, "restart"), [])
            self.assertEqual(kinds(acts, "rent"), [])
            self.assertIn(
                "telemetry_stale",
                [a["reason"] for a in kinds(acts, "blocked")])
        finally:
            db.close()

    def test_no_fault_cancel_right_after_restart(self):
        # restart 1 min ago: fault checks suppressed inside the window
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [listing(server_id=11)])
            add_fleet(db, 1, 11)
            add_rental(db, phase["phase_id"], 1, 11, 7)
            add_obs(db, 1, 1.5, status(cpu=10, gpu=0))
            add_obs(db, 1, 0.5, status(cpu=10, gpu=0))
            add_event(db, phase["phase_id"], 1, "restart", order_id=1,
                      server_id=11)
            acts, _ = actions(db, phase)
            self.assertEqual(kinds(acts, "cancel"), [])
        finally:
            db.close()
        # same fault 2.5 min after the restart: still inside the free
        # window, so it applies
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [listing(server_id=11)])
            add_fleet(db, 1, 11)
            add_rental(db, phase["phase_id"], 1, 11, 8.5)
            add_obs(db, 1, 1.5, status(cpu=10, gpu=0))
            add_obs(db, 1, 0.5, status(cpu=10, gpu=0))
            add_event(db, phase["phase_id"], 2.5, "restart",
                      order_id=1, server_id=11)
            acts, _ = actions(db, phase)
            cancels = kinds(acts, "cancel")
            self.assertEqual(len(cancels), 1)
            self.assertEqual(cancels[0]["reason"], "fault:gpu_its")
        finally:
            db.close()

    def test_dry_slot_bursts_while_incumbent_rides(self):
        # a proven rig that stops producing got handed a dead tree;
        # past its x arm the runner fires a burst of slot-grade probes
        # while the incumbent keeps mining -- the judge decides whether
        # it gets replaced
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [])
            add_fleet(db, 1, 11)
            add_rental(db, phase["phase_id"], 1, 11, 60, x_min=30)
            add_obs(db, 1, 40, status(submitted=5))
            add_obs(db, 1, 0.5, status(submitted=5))
            # 40 min quiet >= x_min(30): burst, no cancel, no restart
            acts, _ = actions(db, phase)
            self.assertEqual(kinds(acts, "restart"), [])
            self.assertEqual(kinds(acts, "cancel"), [])
            bursts = kinds(acts, "burst")
            self.assertEqual(len(bursts), 1)
            self.assertEqual(bursts[0]["trigger"], "dry")
            self.assertEqual(bursts[0]["trigger_order_id"], 1)
            self.assertEqual(bursts[0]["y_min"], 15)
            # a shorter quiet spell stays inside the x arm: nothing
            db2 = make_db()
            try:
                phase2 = add_phase(db2)
                add_snapshot(db2, [])
                add_fleet(db2, 1, 11)
                add_rental(db2, phase2["phase_id"], 1, 11, 60, x_min=30)
                add_obs(db2, 1, 25, status(submitted=5))
                add_obs(db2, 1, 0.5, status(submitted=5))
                acts, _ = actions(db2, phase2)
                self.assertEqual(kinds(acts, "cancel"), [])
                self.assertEqual(kinds(acts, "burst"), [])
            finally:
                db2.close()
        finally:
            db.close()

    def test_never_produced_cancelled_with_retry_burst(self):
        # a rig that has never found a share may simply be unable to
        # hit; cancel fast, and a retry burst fills the vacancy after
        # the slot's y arm
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [])
            add_fleet(db, 1, 11)
            add_rental(db, phase["phase_id"], 1, 11, 30, y_min=15)
            add_obs(db, 1, 20, status(submitted=0))
            add_obs(db, 1, 0.5, status(submitted=0))
            acts, _ = actions(db, phase)
            cancels = kinds(acts, "cancel")
            self.assertEqual(len(cancels), 1)
            self.assertEqual(cancels[0]["reason"], "dry")
            self.assertIsNone(cancels[0]["rerent_at"])
            retry = cancels[0]["retry_burst"]
            self.assertEqual(retry["for_order"], 1)
            self.assertEqual(retry["server_id"], 11)
            self.assertEqual(retry["y_min"], 15)
            self.assertEqual(retry["due_at"],
                             (NOW + timedelta(minutes=15)).isoformat())
        finally:
            db.close()

    def test_dry_cancel_schedules_retry_burst(self):
        # the cancel's retry_burst lands as a burst_scheduled event
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [])
            add_fleet(db, 1, 11)
            add_rental(db, phase["phase_id"], 1, 11, 30, y_min=15)
            add_obs(db, 1, 20, status(submitted=0))
            add_obs(db, 1, 0.5, status(submitted=0))
            acts, _ = actions(db, phase)
            cancel = kinds(acts, "cancel")[0]
            with mock.patch.object(
                    experiment.rent, "cancel_managed_order",
                    return_value=(0, {})):
                experiment.execute(
                    db, ":memory:", "/tmp/env", phase, CFG, [cancel],
                    NOW, live=True, say=lambda *_a: None)
            sched = db.execute(
                "SELECT detail_json FROM experiment_events WHERE "
                "kind='burst_scheduled'").fetchone()
            detail = json.loads(sched["detail_json"])
            self.assertEqual(detail["for_order"], 1)
            self.assertEqual(detail["tier"], "other")
        finally:
            db.close()

    def test_zero_its_no_restart_no_early_burst(self):
        # a rig hashing nothing but sharing inside its x arm is left
        # alone: restarts never helped, and the dry-burst arm hasn't
        # expired yet
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [])
            add_fleet(db, 1, 11)
            add_rental(db, phase["phase_id"], 1, 11, 30)
            add_obs(db, 1, 20, status(submitted=5, cpu=0, gpu=0))
            add_obs(db, 1, 0.5, status(submitted=5, cpu=0, gpu=0))
            acts, _ = actions(db, phase)
            self.assertEqual(kinds(acts, "cancel"), [])
            # zero its is not restarted and not bursted while the
            # last share is inside the x arm
            self.assertEqual(kinds(acts, "restart"), [])
            self.assertEqual(kinds(acts, "burst"), [])
        finally:
            db.close()

    def test_dry_after_x_cancels_with_rerent(self):
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [])
            add_fleet(db, 1, 11)
            add_rental(db, phase["phase_id"], 1, 11, 60, x_min=30,
                       y_min=15)
            add_obs(db, 1, 50, status(submitted=0))
            add_obs(db, 1, 0.5, status(submitted=0))
            add_event(db, phase["phase_id"], 30, "restart", order_id=1,
                      server_id=11)
            acts, _ = actions(db, phase)
            cancels = kinds(acts, "cancel")
            self.assertEqual(len(cancels), 1)
            self.assertEqual(cancels[0]["reason"], "dry_after_x")
            self.assertIsNone(cancels[0]["rerent_at"])
            self.assertEqual(
                cancels[0]["retry_burst"]["due_at"],
                (NOW + timedelta(minutes=15)).isoformat())
        finally:
            db.close()

    def test_due_rerent_and_grace_expiry(self):
        db = make_db()
        try:
            # headroom must cover the rerent's eff burn now that due
            # rerents pass through the same pace gate as fills
            phase = add_phase(db, budget="9.60")
            add_snapshot(db, [listing(server_id=5)])
            add_event(db, phase["phase_id"], 70, "rerent_scheduled",
                      server_id=5,
                      detail={"server_id": 5,
                              "due_at": (NOW - timedelta(
                                  minutes=1)).isoformat()})
            acts, _ = actions(db, phase)
            rents = kinds(acts, "rent")
            self.assertEqual(len(rents), 1)
            self.assertEqual(rents[0]["server_id"], 5)
            self.assertEqual(rents[0]["reason"], "rerent")
        finally:
            db.close()
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [listing(server_id=5)])
            add_event(db, phase["phase_id"], 100, "rerent_scheduled",
                      server_id=5,
                      detail={"server_id": 5,
                              "due_at": (NOW - timedelta(
                                  minutes=70)).isoformat()})
            acts, _ = actions(db, phase)
            self.assertEqual(kinds(acts, "rerent_expired"),
                             [{"type": "rerent_expired",
                               "server_id": 5}])
            self.assertEqual(kinds(acts, "rent"), [])
        finally:
            db.close()

    def test_rerent_cleared_by_rent_failed(self):
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [listing(server_id=5)])
            add_event(db, phase["phase_id"], 30, "rerent_scheduled",
                      server_id=5,
                      detail={"server_id": 5,
                              "due_at": (NOW - timedelta(
                                  minutes=5)).isoformat()})
            add_event(db, phase["phase_id"], 4, "rent_failed",
                      server_id=5)
            acts, _ = actions(db, phase)
            self.assertEqual(kinds(acts, "rent"), [])
            self.assertEqual(kinds(acts, "rerent_expired"), [])
        finally:
            db.close()

    def test_due_rerent_skips_excluded_server(self):
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [listing(server_id=5)])
            # server 5 was fault-cancelled this phase
            add_fleet(db, 1, 5)
            add_rental(db, phase["phase_id"], 1, 5, 60)
            db.execute(
                "UPDATE experiment_rentals SET ended_at=?,"
                " end_reason='fault:gpu_its' WHERE order_id=1",
                (iso(30),))
            db.commit()
            add_event(db, phase["phase_id"], 30, "rerent_scheduled",
                      server_id=5,
                      detail={"server_id": 5,
                              "due_at": (NOW - timedelta(
                                  minutes=5)).isoformat()})
            acts, _ = actions(db, phase)
            self.assertFalse(
                any(a.get("server_id") == 5
                    for a in kinds(acts, "rent")))
        finally:
            db.close()

    def test_due_rerent_does_not_double_count_slot(self):
        db = make_db()
        try:
            # the incumbent burn leaves headroom < s5's eff at the
            # default budget; $12 keeps the rerent inside pace
            phase = add_phase(db, budget="12.00")
            add_history(db, 42, cpu="Intel Xeon E5-2695 v4",
                        fleet_order=999, its_cpu=10, its_gpu=80)
            add_snapshot(db, [
                listing(server_id=5),
                listing(server_id=9, cpu="Intel Xeon E5-2695 v4"),
            ])
            add_event(db, phase["phase_id"], 30, "rerent_scheduled",
                      server_id=5,
                      detail={"server_id": 5,
                              "due_at": (NOW - timedelta(
                                  minutes=5)).isoformat()})
            acts, _ = actions(db, phase)
            rents = kinds(acts, "rent")
            self.assertEqual(len(rents), 2)
            self.assertEqual(rents[0]["reason"], "rerent")
            self.assertEqual(rents[0]["server_id"], 5)
            self.assertEqual(rents[1]["reason"], "fill")
        finally:
            db.close()

    def test_no_zen_quota_fill_by_value(self):
        # the zen quota is gone: a non-zen listing with a measured
        # prior fills ahead of an unproven zen listing
        db = make_db()
        try:
            phase = add_phase(db)
            add_history(db, 30, its_cpu=10, its_gpu=80)
            add_snapshot(db, [
                listing(server_id=7, cpu="AMD Ryzen 9 7950X "
                                       "16-Core Processor", price=4.8),
                listing(server_id=8, cpu="Intel Core i9-10980XE",
                        price=2.4),
            ])
            acts, state = actions(db, phase)
            rents = kinds(acts, "rent")
            self.assertEqual([r["server_id"] for r in rents], [8])
            self.assertEqual(rents[0]["tier"], "other")
            # the zen listing stays in the pool -- it's just unproven
            pool = {c["server_id"]: c
                    for c in experiment._candidates(
                        state, CFG, set(), [])}
            self.assertEqual(pool[7]["tier"], "zen")
            self.assertIsNone(pool[7]["prior"])
        finally:
            db.close()

    def test_fill_orders_by_prior_not_coverage(self):
        # coverage no longer sorts: an unexplored listing that ties a
        # measured one on predicted value/$ loses the id tiebreak
        db = make_db()
        try:
            cfg = dict(CFG, max_rentals=2)
            phase = add_phase(db, cfg=cfg)
            add_fleet(db, 8, 8, deadline_min=-3600)
            add_rental(db, phase["phase_id"], 8, 8, 60, hourly="3.0")
            add_obs(db, 8, 1, status(submitted=5))
            add_history(db, 42, fleet_order=999, its_cpu=10,
                        its_gpu=80)
            add_snapshot(db, [
                listing(server_id=9, cpu="Intel Core i9-10980XE",
                        gpu="RTX 3070"),
                listing(server_id=10, cpu="Intel Xeon E5-2695 v4",
                        gpu="RTX 3070"),
            ])
            acts, _ = actions(db, phase, cfg=cfg)
            rents = kinds(acts, "rent")
            self.assertEqual(len(rents), 1)
            # s9 is measured, s10 only an ISA extrapolation of the
            # same estimate: equal prior -> lowest server_id wins
            self.assertEqual(rents[0]["server_id"], 9)
        finally:
            db.close()

    def test_fill_prefers_known_prior(self):
        # measured prior beats unexplored hardware -- but a same-ISA
        # extrapolation of the identical estimate only ties it
        db = make_db()
        try:
            phase = add_phase(db, cfg=dict(CFG, max_rentals=1))
            add_history(db, 42, fleet_order=999, its_cpu=10,
                        its_gpu=80)
            add_snapshot(db, [
                listing(server_id=9, cpu="Intel Core i9-10980XE",
                        gpu="RTX 3070"),
                listing(server_id=10, cpu="Intel Xeon E5-2695 v4",
                        gpu="RTX 3070"),
            ])
            acts, _ = actions(db, phase, cfg=dict(CFG, max_rentals=1))
            rents = kinds(acts, "rent")
            self.assertEqual(len(rents), 1)
            self.assertEqual(rents[0]["server_id"], 9)
        finally:
            db.close()

    def test_candidates_rank_by_value_per_usd(self):
        # est_its**0.5 per fee-inclusive $: a cheap 3060-class rig
        # outranks a measured 4090, a measured server_prior beats the
        # model priors, and an unknown CPU borrows its ISA median
        db = make_db()
        try:
            phase = add_phase(db)
            add_history(db, 30, gpu="RTX 4090", fleet_order=900,
                        its_cpu=500, its_gpu=800)
            add_history(db, 31, cpu="Intel Xeon E5-2695 v4",
                        fleet_order=901, its_cpu=200, its_gpu=100)
            add_history(db, 32, gpu="RTX 3060", fleet_order=902,
                        its_cpu=100, its_gpu=50)
            add_snapshot(db, [
                listing(server_id=30, gpu="RTX 4090", price=48.0),
                listing(server_id=40, cpu="Intel Xeon E5-2695 v4",
                        price=0.96),
                listing(server_id=50, cpu="Intel Core i7-5960X",
                        price=24.0),
                listing(server_id=60, cpu="AMD Ryzen 9 7950X "
                                       "16-Core Processor",
                        gpu="RTX 3080", price=2.4),
            ])
            _, state = actions(db, phase)
            pool = experiment._candidates(
                state, dict(CFG, max_price_hr="5"), set(), [])
            by_sid = {c["server_id"]: c for c in pool}
            # v(300)/$0.0587 = 295 tops the ranking; v(400)/$1.067 =
            # 18.75 beats v(1300)/$2.117 = 17.04; the AVX512 CPU with
            # no model-class data has no prior at all
            self.assertEqual(
                [c["server_id"] for c in pool], [40, 50, 30, 60])
            self.assertEqual(by_sid[30]["est_its"], 1300)
            self.assertFalse(by_sid[30]["extrapolated"])
            self.assertEqual(by_sid[40]["est_its"], 300)
            self.assertEqual(by_sid[50]["est_its"], 400)
            self.assertTrue(by_sid[50]["extrapolated"])
            self.assertIsNone(by_sid[60]["est_its"])
            self.assertIsNone(by_sid[60]["prior"])
            # no zen quota: the tier label stays, nothing is filtered
            self.assertEqual(by_sid[40]["tier"], "other")
            self.assertEqual(by_sid[60]["tier"], "zen")
        finally:
            db.close()

    def test_cpu_sibling_borrows_measured_prior(self):
        # an unmeasured X3D variant borrows its non-X3D sibling's prior
        # (43) instead of the AVX512 ISA median (24)
        db = make_db()
        try:
            phase = add_phase(db)
            add_history(db, 30, cpu="AMD Ryzen 9 7950X 16-Core Processor",
                        fleet_order=900, its_cpu=43, its_gpu=800)
            add_history(db, 31, cpu="AMD Ryzen 9 7900X 12-Core Processor",
                        fleet_order=901, its_cpu=24, its_gpu=800)
            add_history(db, 32, cpu="Intel Xeon Gold 6138 CPU @ 2.00GHz",
                        fleet_order=902, its_cpu=13, its_gpu=800)
            add_snapshot(db, [
                listing(server_id=40,
                        cpu="AMD Ryzen 9 7950X3D 16-Core Processor",
                        price=2.4),
                listing(server_id=41, cpu="AMD Ryzen 9 9950X3D",
                        price=2.4),
            ])
            _, state = actions(db, phase)
            pool = experiment._candidates(
                state, dict(CFG, max_price_hr="5"), set(), [])
            by_sid = {c["server_id"]: c for c in pool}
            self.assertEqual(by_sid[40]["est_its"], 843)
            self.assertTrue(by_sid[40]["extrapolated"])
            # no measured sibling: falls back to the ISA median
            self.assertEqual(by_sid[41]["est_its"], 824)
        finally:
            db.close()

    def test_loss_mining_rents_below_floor_when_pool_empty(self):
        # s30 sets a floor nothing else reaches but never fits the
        # quote budget; with loss_mining off the vacancy stays empty,
        # on it fills with a below-floor rig. Below slot_min the
        # measured s40 is evidenced and takes the exemption first,
        # leaving s50's bound unfunded -- measured spend before the
        # speculative pick is the intended below-min ordering.
        db = make_db()
        try:
            phase = add_phase(db, budget="0.30", reserve="0")
            add_history(db, 30, gpu="RTX 4090", fleet_order=900,
                        its_cpu=500, its_gpu=800)
            add_history(db, 40, cpu="Intel Core i3-6100",
                        fleet_order=901, its_cpu=10, its_gpu=15)
            add_snapshot(db, [
                listing(server_id=30, gpu="RTX 4090", price=2.4),
                listing(server_id=40, cpu="Intel Core i3-6100",
                        price=0.24),
                listing(server_id=50, cpu="Totally Unknown CPU",
                        price=0.24),
            ])
            acts, _ = actions(db, phase)
            self.assertEqual(kinds(acts, "rent"), [])

            acts, _ = actions(
                db, phase, cfg=dict(CFG, loss_mining=True))
            rents = kinds(acts, "rent")
            self.assertEqual(
                {r["server_id"] for r in rents}, {40})
            self.assertEqual(
                {r["reason"] for r in rents}, {"loss_mining"})
        finally:
            db.close()

    def test_loss_mining_ignores_return_gate_in_ranking(self):
        # s23 fails min_return but loss_mining admits it outright on
        # score -- it wins a vacancy beside the viable s20, flagged
        # loss_mining rather than merely backfilling an empty slot
        db = make_db()
        try:
            cfg = dict(CFG, min_return=5.0, min_value_ratio=0.0,
                       loss_mining=True, score="gross")
            phase = add_phase(db, cfg=cfg)
            add_pool_snap(db, 0, usd_per_counted=0.002)
            add_history(db, 30, fleet_order=900, its_cpu=200,
                        its_gpu=800)
            add_snapshot(db, [listing(server_id=20, price=2.4),
                              listing(server_id=23, price=9.6)])
            acts, _ = actions(db, phase, cfg=cfg)
            by_sid = {r["server_id"]: r for r in kinds(acts, "rent")}
            self.assertEqual(set(by_sid), {20, 23})
            self.assertEqual(by_sid[20]["reason"], "fill")
            self.assertEqual(by_sid[23]["reason"], "loss_mining")
        finally:
            db.close()

    def test_loss_mining_promotes_uneconomic_finder(self):
        # loss_mining drops the min_return promotion gate but a seat
        # still costs standing money: the finder's realized return
        # (0.17x here) must clear min_seat_return. At the default
        # 0.6 floor the vacancy retries instead of seating it; drop
        # the floor below the draw and the same finder promotes.
        db = make_db()
        try:
            cfg = dict(CFG, loss_mining=True)
            phase = add_phase(db, cfg=cfg)
            add_pool_snap(db, 0, shares=1000, counted=500,
                          usd_per_counted=0.001)
            add_snapshot(db, [])
            burst_id = add_burst(db, phase["phase_id"], 6,
                                 trigger="dry_retry",
                                 trigger_order_id=1, n_target=2)
            add_fleet(db, 10, 20)
            add_rental(db, phase["phase_id"], 10, 20, 8.5,
                       role="probe", burst_id=burst_id,
                       hourly="0.10")
            add_obs(db, 10, 5, status(submitted=1, cpu=50))
            add_obs(db, 10, 0.5, status(submitted=3, cpu=50))
            acts, _ = actions(db, phase, cfg=cfg)
            self.assertEqual(kinds(acts, "judge")[0]["verdict"],
                             "retry_again")
            acts, _ = actions(
                db, phase, cfg=dict(cfg, min_seat_return=0.1))
            judge = kinds(acts, "judge")[0]
            self.assertEqual(judge["verdict"], "promoted")
            self.assertEqual(judge["promote"], 10)
        finally:
            db.close()

    def test_set_phase_config_toggles_loss_mining(self):
        db = make_db()
        try:
            phase = add_phase(db)
            self.assertFalse(
                json.loads(phase["config_json"])["loss_mining"])
            cfg = experiment.set_phase_config(db, loss_mining=True)
            self.assertTrue(cfg["loss_mining"])
            stored = json.loads(db.execute(
                "SELECT config_json FROM experiment_phases"
            ).fetchone()["config_json"])
            self.assertTrue(stored["loss_mining"])
            with self.assertRaises(ValueError):
                experiment.set_phase_config(db, bogus_key=1)
        finally:
            db.close()

    def test_its_total_smoothed_over_window(self):
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [])
            add_fleet(db, 1, 11)
            add_rental(db, phase["phase_id"], 1, 11, 30)
            # warm-up spikes (first 2 min after t0) are excluded; the
            # rest of the window alternates wildly -> the median, not
            # the last sample, is what judges see
            add_obs(db, 1, 9, status(submitted=1, cpu=2000, gpu=3000))
            add_obs(db, 1, 8, status(submitted=2, cpu=2000, gpu=3000))
            for i, t in enumerate((7, 6, 5, 4, 3, 2, 1, 0.5)):
                v = 600 if i % 2 == 0 else 5
                add_obs(db, 1, t, status(submitted=2, cpu=v, gpu=0))
            _, state = actions(db, phase)
            r = state["rentals"][0]
            self.assertEqual(r["its_total"], 302.5)
            self.assertEqual(r["its_last"], 5)
            # no good obs inside the window at all -> last sample
            db2 = make_db()
            try:
                phase2 = add_phase(db2)
                add_snapshot(db2, [])
                add_fleet(db2, 1, 11)
                add_rental(db2, phase2["phase_id"], 1, 11, 30)
                add_obs(db2, 1, 30, status(submitted=1, cpu=900))
                add_obs(db2, 1, 20, status(submitted=2, cpu=50))
                _, state2 = actions(db2, phase2)
                self.assertEqual(
                    state2["rentals"][0]["its_total"], 50 + 80)
            finally:
                db2.close()
        finally:
            db.close()

    def test_pace_math(self):
        db = make_db()
        try:
            phase = add_phase(db, budget="10")
            add_snapshot(db, [])
            add_fleet(db, 1, 11, deadline_min=-120)  # 2h left
            add_rental(db, phase["phase_id"], 1, 11, 30, hourly="0.50")
            add_obs(db, 1, 0.5, status(submitted=5))
            # probes don't count toward the slot burn or active_left;
            # their full-term quote is held back into money
            add_fleet(db, 2, 12)
            add_rental(db, phase["phase_id"], 2, 12, 5, role="probe",
                       hourly="9.99")
            add_obs(db, 2, 0.5, status(submitted=0))
            _, state = actions(db, phase)
            p = experiment.pace(state, NOW, CFG)
            hours = (experiment.epoch_end(NOW) - NOW
                     ).total_seconds() / 3600
            self.assertAlmostEqual(p["hours_to_epoch"], hours)
            # committed = 2 x $0.70 quote = $1.40 -> remaining $8.60;
            # the slot holds $1.00 of already-committed burn
            self.assertAlmostEqual(p["active_left_usd"], 0.50 * 2.0)
            self.assertAlmostEqual(p["probe_hold_usd"], 0.70)
            self.assertAlmostEqual(p["money"], 8.60 + 1.0 + 0.70)
            self.assertAlmostEqual(p["sustainable_hr"], 10.30 / hours)
            self.assertAlmostEqual(p["slot_burn_hr"], 0.50)
            self.assertAlmostEqual(p["headroom_hr"],
                                   10.30 / hours - 0.50)
            # a deadline-less slot contributes nothing to active_left
            db.execute("UPDATE fleet_rentals SET deadline_at=NULL")
            db.commit()
            _, state = actions(db, phase)
            self.assertEqual(
                experiment.pace(state, NOW, CFG)["active_left_usd"],
                0.0)
        finally:
            db.close()

    def test_fill_picks_best_set_within_headroom(self):
        # two vacancies: the highest-value set inside pace headroom
        # fills; when only a singleton fits, just it fills; when
        # nothing fits, the empty slot is noted rather than spent
        db = make_db()
        try:
            cfg = dict(CFG, max_rentals=2)
            phase = add_phase(db, cfg=cfg)
            add_history(db, 30, fleet_order=900, its_cpu=10, its_gpu=80)
            add_snapshot(db, [
                listing(server_id=20, price=2.4),   # $0.105/h
                listing(server_id=21, price=2.4),
                listing(server_id=22, price=48.0),  # $2.10/h
            ])
            acts, _ = actions(db, phase, cfg=cfg)
            # headroom ~$1.66/h: the cheap pair (combined $0.24/h)
            # beats any set containing the pricey rig on value
            self.assertEqual(
                [r["server_id"] for r in kinds(acts, "rent")],
                [20, 21])
        finally:
            db.close()
        # a slot already burning leaves room for one more only
        db = make_db()
        try:
            cfg = dict(CFG, max_rentals=2)
            phase = add_phase(db, cfg=cfg, budget="122")
            add_history(db, 30, fleet_order=900, its_cpu=10, its_gpu=80)
            add_fleet(db, 1, 11)
            add_rental(db, phase["phase_id"], 1, 11, 60, hourly="1.60")
            add_obs(db, 1, 0.5, status(submitted=5))
            add_snapshot(db, [listing(server_id=20, price=2.4),
                              listing(server_id=21, price=2.4)])
            acts, _ = actions(db, phase, cfg=cfg)
            # sustainable ~$1.82/h - $1.60/h burn = ~$0.22/h headroom:
            # the pair needs $0.24/h, a singleton $0.12/h fits
            self.assertEqual(
                [r["server_id"] for r in kinds(acts, "rent")], [20])
        finally:
            db.close()
        # burning more than sustainable with a vacancy at slot_min:
        # nothing fits, noted (below min the exemption would fill
        # anyway; static keeps slot_target above the seated fleet)
        db = make_db()
        try:
            cfg = dict(CFG, max_rentals=2, slot_min=1,
                       slot_mode="static")
            phase = add_phase(db, cfg=cfg)
            add_history(db, 30, fleet_order=900, its_cpu=10, its_gpu=80)
            add_fleet(db, 1, 11)
            add_rental(db, phase["phase_id"], 1, 11, 60, hourly="5.0")
            add_obs(db, 1, 0.5, status(submitted=5))
            add_snapshot(db, [listing(server_id=20, price=2.4)])
            notes = []
            acts, _ = actions(db, phase, cfg=cfg, say=notes.append)
            self.assertEqual(kinds(acts, "rent"), [])
            self.assertTrue(any(
                "none fits pace headroom" in n and "s20" in n
                for n in notes))
        finally:
            db.close()

    def test_fill_skips_listings_below_value_floor(self):
        # same hardware at twice the price is ~54% of the best v/$:
        # headroom would fund it, the 0.7 floor refuses it and the
        # second slot stays empty; with the floor off it fills
        for ratio, expected in ((0.7, [20]), (0.0, [20, 23])):
            db = make_db()
            try:
                cfg = dict(CFG, max_rentals=2, min_value_ratio=ratio)
                phase = add_phase(db, cfg=cfg)
                add_history(db, 30, fleet_order=900, its_cpu=10,
                            its_gpu=80)
                add_snapshot(db, [listing(server_id=20, price=2.4),
                                  listing(server_id=23, price=4.8)])
                notes = []
                acts, _ = actions(db, phase, cfg=cfg, say=notes.append)
                self.assertEqual(
                    [r["server_id"] for r in kinds(acts, "rent")],
                    expected)
                if ratio:
                    self.assertTrue(any("below_floor=1" in n
                                        for n in notes))
            finally:
                db.close()

    def test_probe_fill_skips_listings_below_value_floor(self):
        db = make_db()
        try:
            phase = add_phase(db)
            add_history(db, 30, fleet_order=900, its_cpu=10, its_gpu=80)
            add_snapshot(db, [listing(server_id=23, price=4.8),
                              listing(server_id=20, price=2.4)])
            add_burst(db, phase["phase_id"], 1, trigger="manual",
                      n_target=2)
            acts, _ = actions(db, phase,
                              cfg=dict(CFG, min_value_ratio=0.7))
            probes = [r for r in kinds(acts, "rent")
                      if r.get("role") == "probe"]
            self.assertEqual([r["server_id"] for r in probes], [20])
            # the floor stays relative: once s20 is taken, s23 is the
            # best remaining listing... but s20 is only "chosen", so
            # the reference best is still s20 and s23 never qualifies
            self.assertNotIn(23, [r["server_id"]
                                  for r in kinds(acts, "rent")])
        finally:
            db.close()

    def test_last_share_at_reset_logic(self):
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [])
            add_fleet(db, 1, 11)
            add_rental(db, phase["phase_id"], 1, 11, 30)
            add_obs(db, 1, 20, status(submitted=5))
            add_obs(db, 1, 15, status(submitted=3))   # reset, >0: arrives
            add_obs(db, 1, 10, status(submitted=0))   # reset to 0: no
            add_obs(db, 1, 5, status(submitted=2))    # > prev: arrives
            _, state = actions(db, phase)
            rental = state["rentals"][0]
            self.assertEqual(rental["last_share_at"],
                             NOW - timedelta(minutes=5))
            self.assertEqual(
                [a for _, a in rental["share_arrivals"]], [5, 3, 2])
        finally:
            db.close()

    def _scored(self, submitted, score_sum, score_med=3000,
                score_n=None):
        st = status(submitted=submitted)
        st["shares"]["score_sum"] = score_sum
        st["shares"]["score_med"] = score_med
        st["shares"]["score_n"] = (
            submitted if score_n is None else score_n)
        return st

    def test_score_fields_on_rental_row(self):
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [])
            add_fleet(db, 1, 11)
            add_rental(db, phase["phase_id"], 1, 11, 30)
            add_obs(db, 1, 20, self._scored(5, 17000, 3400))
            add_obs(db, 1, 10, self._scored(8, 27800, 3450))
            _, state = actions(db, phase)
            rental = state["rentals"][0]
            self.assertEqual(
                [a for _, a in rental["score_arrivals"]],
                [17000, 10800])
            self.assertAlmostEqual(rental["score_per_share"],
                                   27800 / 8, places=3)
            self.assertEqual(rental["score_med"], 3450)
        finally:
            db.close()

    def test_score_arrivals_reset(self):
        # the stats file restarts at the next epoch/roll: a drop in
        # score_sum is a new counter, and the resumed baseline keeps
        # pre-restart sums out of the stint measurement
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [])
            add_fleet(db, 1, 11)
            add_rental(db, phase["phase_id"], 1, 11, 30)
            add_obs(db, 1, 25, self._scored(4, 14000))
            add_obs(db, 1, 15, self._scored(2, 7000))   # counter reset
            add_obs(db, 1, 5, self._scored(3, 10500))
            _, state = actions(db, phase)
            rental = state["rentals"][0]
            self.assertEqual(
                [a for _, a in rental["score_arrivals"]],
                [14000, 7000, 3500])
        finally:
            db.close()

    def test_history_stats_score_ref_and_detail(self):
        db = make_db()
        try:
            add_snapshot(db, [])
            add_history(db, 30, gpu="RTX 4090", fleet_order=900)
            # overwrite the second obs with score-bearing status
            st = status(submitted=5)
            st["shares"].update({"score_sum": 17500,
                                 "score_med": 3500, "score_n": 5})
            db.execute(
                "UPDATE rig_observations SET status_json=?"
                " WHERE order_id=900", (json.dumps(st),))
            db.commit()
            stats = experiment.history_stats(
                db, experiment.server_listings(db))
            self.assertEqual(stats["score_ref"], 3500)
            detail = stats["gpu_detail"]["RTX 4090"]
            self.assertEqual(detail["score_per_share"], 3500)
            self.assertEqual(detail["score_n"], 1)
        finally:
            db.close()

    def test_realized_usd_h_score_weighting(self):
        vm = {"P": 0.01, "w_bar": 1.0}
        t0 = NOW - timedelta(hours=1)
        base = {"t0": t0, "its_total": 100,
                "share_arrivals": [(NOW - timedelta(minutes=30), 10)]}
        hours = (NOW - (t0 + timedelta(minutes=2))).total_seconds() / 3600
        want_base = (10 / hours) * 1.0 * 0.01
        # at the reference, weighting is neutral
        r = dict(base, score_per_share=3500)
        self.assertAlmostEqual(
            experiment.realized_usd_h(r, vm, NOW, score_ref=3500),
            want_base)
        # below-reference scoring rigs earn proportionally less
        r = dict(base, score_per_share=1750)
        self.assertAlmostEqual(
            experiment.realized_usd_h(r, vm, NOW, score_ref=3500),
            want_base * 0.5)
        # no score telemetry: plain share rate, no penalty
        r = dict(base)
        self.assertAlmostEqual(
            experiment.realized_usd_h(r, vm, NOW, score_ref=3500),
            want_base)
        # no reference yet (nothing scored fleet-wide): same
        r = dict(base, score_per_share=3500)
        self.assertAlmostEqual(
            experiment.realized_usd_h(r, vm, NOW), want_base)

    def test_phase_end_and_stale_market_block_rents(self):
        db = make_db()
        try:
            ended = add_phase(db, ends_min=10)
            add_snapshot(db, [listing(server_id=5)])
            acts, _ = actions(db, ended)
            self.assertIn("phase",
                          [a["reason"] for a in kinds(acts, "blocked")])
            self.assertEqual(kinds(acts, "rent"), [])
        finally:
            db.close()
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [listing(server_id=5)], minutes_ago=20)
            acts, _ = actions(db, phase)
            self.assertIn("stale_market",
                          [a["reason"] for a in kinds(acts, "blocked")])
        finally:
            db.close()
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [listing(server_id=5)])
            db.execute(
                "INSERT INTO rental_attempts(attempt_id, server_id,"
                " alias, before_ids_json, quote_json, state,"
                " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                ("a1", 5, "rig-x-a1", "[]", "{}", "creating",
                 iso(2), iso(2)))
            db.commit()
            acts, _ = actions(db, phase)
            self.assertIn("pending",
                          [a["reason"] for a in kinds(acts, "blocked")])
            self.assertEqual(kinds(acts, "rent"), [])
        finally:
            db.close()

    def test_ended_detection_closes_rental(self):
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [])
            add_fleet(db, 1, 11, state="cancelled")
            add_rental(db, phase["phase_id"], 1, 11, 30)
            _, state = actions(db, phase)
            self.assertEqual(state["rentals"], [])
            row = db.execute(
                "SELECT ended_at, end_reason FROM experiment_rentals "
                "WHERE order_id=1").fetchone()
            self.assertIsNotNone(row["ended_at"])
            self.assertEqual(row["end_reason"], "cancelled")
            kinds_row = db.execute(
                "SELECT kind FROM experiment_events WHERE "
                "kind='ended'").fetchone()
            self.assertIsNotNone(kinds_row)
        finally:
            db.close()

    def test_probe_guard_suppresses_bursts(self):
        """A probe near its free-window deadline freezes rentals AND
        new bursts -- a burst that starts but can't fill judges empty
        and burns an upgrade slot for nothing."""
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [listing(20)])
            # producing slot quiet past its x arm wants a dry burst
            add_fleet(db, 1, 11)
            add_rental(db, phase["phase_id"], 1, 11, 40)
            add_obs(db, 1, 35, status(submitted=5))
            add_obs(db, 1, 0.5, status(submitted=5))
            acts, _ = actions(db, phase)
            self.assertEqual(len(kinds(acts, "burst")), 1)  # sanity
            # a probe at guard age: no burst may start now
            add_fleet(db, 2, 22)
            add_rental(db, phase["phase_id"], 2, 22, 5, role="probe",
                       burst_id=add_burst(db, phase["phase_id"], 10))
            add_obs(db, 2, 0.5, status(submitted=1))
            # the burst above is judged first so probe_guard is the
            # only suppressor
            db.execute("UPDATE experiment_bursts SET judged_at=?",
                       (iso(0),))
            db.commit()
            acts, _ = actions(db, phase)
            self.assertEqual(kinds(acts, "burst"), [])
            self.assertIn("probe_guard",
                          [a["reason"] for a in kinds(acts, "blocked")])
        finally:
            db.close()

    def test_quote_rejected_server_excluded_from_fill(self):
        """A server whose quote failed is out of the candidate pool
        for quote_reject_min, then eligible again."""
        db = make_db()
        try:
            phase = add_phase(db)
            add_history(db, 42, fleet_order=999, its_cpu=10, its_gpu=80)
            add_snapshot(db, [listing(20), listing(21)])
            add_event(db, phase["phase_id"], 10, "rent_failed",
                      server_id=20,
                      detail={"stage": "quote", "error": "ValueError"})
            acts, _ = actions(db, phase)
            rents = kinds(acts, "rent")
            self.assertEqual(len(rents), 1)
            self.assertEqual(rents[0]["server_id"], 21)
            # a real (provision-stage) failure is still permanent,
            # even past the quote cooldown
            db2 = make_db()
            try:
                phase2 = add_phase(db2)
                add_history(db2, 42, fleet_order=999, its_cpu=10,
                            its_gpu=80)
                add_snapshot(db2, [listing(20), listing(21)])
                add_event(db2, phase2["phase_id"], 90, "rent_failed",
                          server_id=20,
                          detail={"info": {"stage": "provision"}})
                acts, _ = actions(db2, phase2)
                rents = kinds(acts, "rent")
                self.assertEqual(len(rents), 1)
                self.assertEqual(rents[0]["server_id"], 21)
            finally:
                db2.close()
            # and a quote rejection expires after quote_reject_min
            db3 = make_db()
            try:
                phase3 = add_phase(db3)
                add_history(db3, 42, fleet_order=999, its_cpu=10,
                            its_gpu=80)
                add_snapshot(db3, [listing(20), listing(21)])
                add_event(db3, phase3["phase_id"], 90, "rent_failed",
                          server_id=20,
                          detail={"stage": "quote",
                                  "error": "ValueError"})
                acts, _ = actions(db3, phase3)
                self.assertTrue(any(a["server_id"] == 20
                                    for a in kinds(acts, "rent")))
            finally:
                db3.close()
        finally:
            db.close()

    def test_low_reliability_rejected(self):
        """Listings below min_reliability -- or with no rating at
        all -- never reach the pool that fills, bursts, upgrades and
        the epoch plan all draw from."""
        db = make_db()
        try:
            phase = add_phase(db)
            add_history(db, 42, fleet_order=999, its_cpu=10, its_gpu=80)
            add_snapshot(db, [listing(20, reliability=0.60),
                              listing(21, reliability=None),
                              listing(22, reliability=0.95)])
            acts, state = actions(db, phase)
            rents = kinds(acts, "rent")
            self.assertEqual(len(rents), 1)
            self.assertEqual(rents[0]["server_id"], 22)
            rejects = {}
            pool = experiment._candidates(state, CFG, set(), [],
                                          rejects=rejects)
            self.assertEqual([c["server_id"] for c in pool], [22])
            self.assertEqual(rejects["low_rel"], 2)
            cfg = dict(CFG, min_reliability=0.0)
            pool = experiment._candidates(state, cfg, set(), [])
            self.assertEqual(len(pool), 3)
        finally:
            db.close()


class ExecutorTests(unittest.TestCase):
    def test_dry_run_makes_no_external_calls(self):
        db = make_db()
        try:
            phase = add_phase(db)
            acts = [
                {"type": "cancel", "order_id": 1, "server_id": 11,
                 "reason": "unreachable", "rerent_at": None},
                {"type": "rent", "server_id": 5, "reason": "fill",
                 "tier": "zen"},
            ]
            with mock.patch.object(
                    experiment.rent, "cancel_managed_order"
                    ) as cancel_mock, \
                    mock.patch.object(
                        experiment.rent, "quote_order") as quote_mock, \
                    mock.patch.object(
                        experiment.rent, "create_order_flow"
                    ) as create_mock, \
                    mock.patch.object(
                        experiment.orders, "fetch_wallets"
                    ) as wallet_mock, \
                    mock.patch.object(
                        experiment.rent, "load_secrets") as secrets_mock:
                experiment.execute(
                    db, ":memory:", "/tmp/env", phase, CFG, acts, NOW,
                    live=False, say=lambda *_a: None)
            for m in (cancel_mock, quote_mock, create_mock,
                      wallet_mock, secrets_mock):
                m.assert_not_called()
            rows = db.execute(
                "SELECT kind FROM experiment_events").fetchall()
            self.assertEqual(len(rows), 2)
            self.assertTrue(all(r["kind"] == "dryrun" for r in rows))
        finally:
            db.close()

    def test_budget_blocks_rent(self):
        db = make_db()
        try:
            phase = add_phase(db, budget="0.50")
            add_snapshot(db, [listing(server_id=5)])
            add_fleet(db, 1, 11)
            add_rental(db, phase["phase_id"], 1, 11, 30)
            with mock.patch.object(
                    experiment.rent, "quote_order") as quote_mock:
                experiment.execute(
                    db, ":memory:", "/tmp/env", phase, CFG,
                    [{"type": "rent", "server_id": 5, "reason": "fill",
                      "tier": "zen"}], NOW, live=True,
                    say=lambda *_a: None)
            quote_mock.assert_not_called()
            row = db.execute(
                "SELECT kind, detail_json FROM experiment_events"
                ).fetchone()
            self.assertEqual(row["kind"], "blocked")
            self.assertEqual(
                json.loads(row["detail_json"])["reason"], "budget")
        finally:
            db.close()

    def test_balance_blocks_rent(self):
        db = make_db()
        try:
            phase = add_phase(db, budget="100")
            add_snapshot(db, [listing(server_id=5)])
            with mock.patch.object(
                    experiment.rent, "load_secrets",
                    return_value=("KEY", None)), \
                    mock.patch.object(
                        experiment.orders, "fetch_wallets",
                        return_value={"code": 0, "wallets": [
                            {"name": "USD-Blockchain", "balance": 0.01}
                        ]}), \
                    mock.patch.object(
                        experiment.rent, "quote_order") as quote_mock:
                experiment.execute(
                    db, ":memory:", "/tmp/env", phase, CFG,
                    [{"type": "rent", "server_id": 5, "reason": "fill",
                      "tier": "zen"}], NOW, live=True,
                    say=lambda *_a: None)
            quote_mock.assert_not_called()
            row = db.execute(
                "SELECT kind, detail_json FROM experiment_events"
                ).fetchone()
            self.assertEqual(row["kind"], "blocked")
            self.assertEqual(
                json.loads(row["detail_json"])["reason"], "balance")
        finally:
            db.close()

    def test_cancel_then_rerent_scheduled(self):
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [])
            add_fleet(db, 1, 11)
            add_rental(db, phase["phase_id"], 1, 11, 60)
            due = (NOW + timedelta(minutes=15)).isoformat()
            with mock.patch.object(
                    experiment.rent, "cancel_managed_order",
                    return_value=(0, {"order_id": 1})) as cancel_mock:
                experiment.execute(
                    db, ":memory:", "/tmp/env", phase, CFG,
                    [{"type": "cancel", "order_id": 1, "server_id": 11,
                      "tier": "zen", "reason": "dry_after_x",
                      "rerent_at": due}],
                    NOW, live=True, say=lambda *_a: None)
            cancel_mock.assert_called_once()
            row = db.execute(
                "SELECT ended_at, end_reason FROM experiment_rentals "
                "WHERE order_id=1").fetchone()
            self.assertEqual(row["end_reason"], "dry_after_x")
            events = [r["kind"] for r in db.execute(
                "SELECT kind FROM experiment_events ORDER BY id")]
            self.assertIn("cancel", events)
            self.assertIn("rerent_scheduled", events)
            sched = db.execute(
                "SELECT detail_json FROM experiment_events WHERE "
                "kind='rerent_scheduled'").fetchone()
            self.assertEqual(json.loads(sched["detail_json"])["due_at"],
                             due)
            self.assertEqual(
                json.loads(sched["detail_json"])["tier"], "zen")
        finally:
            db.close()


class BurstTests(unittest.TestCase):
    def _dry_slot(self, db, phase, order_id=1, server_id=11):
        add_fleet(db, order_id, server_id)
        add_rental(db, phase["phase_id"], order_id, server_id, 60)
        add_obs(db, order_id, 40, status(submitted=5))
        add_obs(db, order_id, 0.5, status(submitted=5))

    def test_dry_burst_blocked_only_by_unjudged(self):
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [listing(server_id=20)])
            self._dry_slot(db, phase)
            acts, _ = actions(db, phase)
            bursts = kinds(acts, "burst")
            self.assertEqual(len(bursts), 1)
            self.assertEqual(bursts[0]["trigger"], "dry")
            self.assertEqual(bursts[0]["trigger_order_id"], 1)
            self.assertEqual(bursts[0]["n"], CFG["burst_max"])
            # an unjudged burst doesn't block: concurrent auditions
            # run one per seat (cap = max_rentals)
            add_burst(db, phase["phase_id"], 2)
            acts, _ = actions(db, phase)
            self.assertEqual(len(kinds(acts, "burst")), 1)
            # at the cap -- two unjudged for two seats -- nothing emits
            add_burst(db, phase["phase_id"], 1)
            acts, _ = actions(db, phase)
            self.assertEqual(kinds(acts, "burst"), [])
            # a judged burst does not block: no cooldown, the y arm
            # paces retries instead
            db2 = make_db()
            try:
                phase2 = add_phase(db2)
                add_snapshot(db2, [listing(server_id=20)])
                self._dry_slot(db2, phase2)
                add_burst(db2, phase2["phase_id"], 30, judged=True)
                acts, _ = actions(db2, phase2)
                self.assertEqual(kinds(acts, "cancel"), [])
                self.assertEqual(len(kinds(acts, "burst")), 1)
            finally:
                db2.close()
        finally:
            db.close()

    def test_manual_burst_consumed_once(self):
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [listing(server_id=20)])
            add_event(db, phase["phase_id"], 1, "burst_requested",
                      detail={"n": 2})
            acts, _ = actions(db, phase)
            bursts = kinds(acts, "burst")
            self.assertEqual(len(bursts), 1)
            self.assertEqual(bursts[0]["trigger"], "manual")
            self.assertEqual(bursts[0]["n"], 2)
            add_event(db, phase["phase_id"], 0.5, "burst_started",
                      detail={"burst_id": 1})
            acts, _ = actions(db, phase)
            self.assertEqual(kinds(acts, "burst"), [])
        finally:
            db.close()

    def test_probe_renting_one_per_cycle_slot_grade(self):
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [
                listing(server_id=20, price=2.4),
                listing(server_id=21, price=4.0),
            ])
            burst_id = add_burst(db, phase["phase_id"], 1, n_target=3)
            acts, _ = actions(db, phase)
            probes = [r for r in kinds(acts, "rent")
                      if r.get("role") == "probe"]
            self.assertEqual(len(probes), 1)
            self.assertEqual(probes[0]["burst_id"], burst_id)
            self.assertEqual(probes[0]["server_id"], 20)
            # probes are prospective slots: full slot term, real tier
            self.assertEqual(probes[0]["hours"], CFG["hours"])
            self.assertEqual(probes[0]["tier"], "other")
            for oid, sid in ((10, 20), (11, 21), (12, 9)):
                add_fleet(db, oid, sid)
                add_rental(db, phase["phase_id"], oid, sid, 1,
                           role="probe", burst_id=burst_id)
                add_obs(db, oid, 0.5, status(submitted=0))
                acts, _ = actions(db, phase)
                probes_now = [r for r in kinds(acts, "rent")
                              if r.get("role") == "probe"]
                if oid == 10:
                    self.assertEqual(len(probes_now), 1)
                    self.assertEqual(probes_now[0]["server_id"], 21)
            acts, _ = actions(db, phase)
            self.assertEqual(kinds(acts, "rent"), [])
        finally:
            db.close()

    def test_probe_prefers_burst_target_server(self):
        # an upgrade burst auditions the listing that triggered it,
        # not whatever tops the generic coverage-first ranking
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [
                listing(server_id=20, price=2.4),
                listing(server_id=21, price=4.0),
            ])
            burst_id = add_burst(db, phase["phase_id"], 1,
                                 trigger="upgrade", n_target=1)
            experiment.emit(
                db, NOW, phase["phase_id"], None, None,
                "burst_started",
                {"burst_id": burst_id, "trigger": "upgrade", "n": 1,
                 "server_id": 21}, say=lambda *_a: None)
            acts, _ = actions(db, phase)
            probes = [r for r in kinds(acts, "rent")
                      if r.get("role") == "probe"]
            self.assertEqual(len(probes), 1)
            self.assertEqual(probes[0]["server_id"], 21)
        finally:
            db.close()

    def test_unaffordable_candidates_note_instead_of_rents(self):
        # remaining budget cannot cover any quote: decide must not
        # emit futile rent actions that churn rent_failed rows
        db = make_db()
        try:
            cfg = dict(CFG)
            phase = add_phase(db, budget="0.50", cfg=cfg)
            add_snapshot(db, [listing(server_id=20, price=2.4)])
            # est bound ~$0.83 > remaining $0.50
            add_burst(db, phase["phase_id"], 1, n_target=1)
            notes = []
            acts, _ = actions(db, phase, cfg=cfg, say=notes.append)
            self.assertEqual(kinds(acts, "rent"), [])
            self.assertTrue(
                any("none affordable" in n for n in notes))
        finally:
            db.close()

    def test_upgrade_burst_skipped_when_probe_unaffordable(self):
        # a dominant listing still isn't worth an audition the budget
        # cannot cover
        db = make_db()
        try:
            cfg = dict(CFG, max_rentals=1)
            phase = add_phase(db, budget="0.80", cfg=cfg)
            self._producing_slot(db, phase)  # commits ~$0.73
            strong = self._server_with_history(
                db, 20, "AMD Ryzen 9 9950X 16-Core Processor",
                "RTX 4090")
            strong["price"] = {"usd": {"on_demand_usd": 0.5,
                                       "spot": 0}}
            add_snapshot(db, [strong])
            # a long deadline keeps the incumbent's committed burn
            # inside the pace cap so the quote check is what blocks
            db.execute("UPDATE fleet_rentals SET deadline_at=?",
                       (iso(-3600),))
            db.commit()
            notes = []
            acts, _ = actions(db, phase, cfg=cfg, say=notes.append)
            self.assertEqual(kinds(acts, "burst"), [])
            self.assertTrue(
                any("dominant but est" in n for n in notes))
        finally:
            db.close()

    def test_probes_exceed_slots_not_total_cap(self):
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [listing(server_id=20)])
            burst_id = add_burst(db, phase["phase_id"], 1, n_target=4)
            # 2 slot + 4 probe active = max_rentals + burst_max cap
            for oid in (30, 31):
                add_fleet(db, oid, 30 + oid - 30)
                add_rental(db, phase["phase_id"], oid, 30 + oid - 30,
                           20)
                add_obs(db, oid, 0.5, status(submitted=1))
            for oid, sid in ((40, 40), (41, 41), (42, 42), (43, 43)):
                add_fleet(db, oid, sid)
                add_rental(db, phase["phase_id"], oid, sid, 3,
                           role="probe", burst_id=burst_id)
                add_obs(db, oid, 0.5, status(submitted=1))
            acts, _ = actions(db, phase)
            self.assertEqual(kinds(acts, "rent"), [])
            # one probe ending frees a total slot: probes still rent
            # even though both max_rentals slots are full
            db.execute(
                "UPDATE experiment_rentals SET ended_at=? WHERE "
                "order_id=43", (iso(0),))
            db.commit()
            acts, _ = actions(db, phase)
            rents = kinds(acts, "rent")
            self.assertEqual(len(rents), 1)
            self.assertEqual(rents[0]["role"], "probe")
        finally:
            db.close()

    def test_no_restarts_for_probes(self):
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [])
            burst_id = add_burst(db, phase["phase_id"], 3)
            add_fleet(db, 10, 20)
            add_rental(db, phase["phase_id"], 10, 20, 6, role="probe",
                       burst_id=burst_id)
            add_obs(db, 10, 2, status(submitted=0))
            add_obs(db, 10, 0.5, status(submitted=0))
            acts, _ = actions(db, phase)
            self.assertEqual(kinds(acts, "restart"), [])
            # and a dry probe still gets none
            add_obs(db, 10, 30, status(submitted=5))
            add_fleet(db, 11, 21)
            add_rental(db, phase["phase_id"], 11, 21, 30, role="probe",
                       burst_id=burst_id)
            add_obs(db, 11, 20, status(submitted=5))
            add_obs(db, 11, 0.5, status(submitted=5))
            acts, _ = actions(db, phase)
            self.assertEqual(kinds(acts, "restart"), [])
        finally:
            db.close()

    def test_judge_dry_burst_promotes_best_finder(self):
        # the dry incumbent produced nothing during the burst; the best
        # finder is promoted to slot and everyone else is cancelled
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [])
            self._dry_slot(db, phase)
            burst_id = add_burst(db, phase["phase_id"], 6,
                                 trigger="dry", trigger_order_id=1,
                                 n_target=3)
            for oid, sid, hourly in ((10, 20, "0.05"),
                                     (11, 21, "0.10"),
                                     (12, 22, "0.10")):
                add_fleet(db, oid, sid)
                add_rental(db, phase["phase_id"], oid, sid, 8.5,
                           role="probe", burst_id=burst_id,
                           hourly=hourly)
            add_obs(db, 10, 5, status(submitted=1, cpu=50))  # finder
            add_obs(db, 10, 0.5, status(submitted=3, cpu=50))
            add_obs(db, 11, 5, status(submitted=0))
            add_obs(db, 11, 0.5, status(submitted=0))
            add_obs(db, 12, 5, status(submitted=0))
            add_obs(db, 12, 0.5, status(submitted=0))
            acts, _ = actions(db, phase)
            judges = kinds(acts, "judge")
            self.assertEqual(len(judges), 1)
            judge = judges[0]
            self.assertEqual(judge["verdict"], "promoted")
            self.assertEqual(judge["promote"], 10)
            cancel_orig = judge["cancel_original"]
            self.assertEqual(
                (cancel_orig["order_id"], cancel_orig["server_id"],
                 cancel_orig["tier"], cancel_orig["reason"]),
                (1, 11, "other", "replaced"))
            self.assertEqual([c["order_id"] for c in judge["cancel"]],
                             [11, 12])
            with mock.patch.object(
                    experiment.rent, "cancel_managed_order",
                    return_value=(0, {})) as cancel_mock:
                experiment.execute(
                    db, ":memory:", "/tmp/env", phase, CFG, [judge],
                    NOW, live=True, say=lambda *_a: None)
            self.assertEqual(cancel_mock.call_count, 3)
            # judge-issued cancels carry the rental's outcome context
            for ev in db.execute(
                    "SELECT detail_json FROM experiment_events WHERE "
                    "kind='cancel'"):
                d = json.loads(ev["detail_json"])
                self.assertIsNotNone(d["its"])
                self.assertIsNotNone(d["shares"])
                self.assertIsNotNone(d["age_min"])
            roles = {r["order_id"]: (r["role"], r["end_reason"])
                     for r in db.execute(
                         "SELECT order_id, role, end_reason FROM "
                         "experiment_rentals")}
            self.assertEqual(roles[10], ("slot", None))
            self.assertEqual(roles[1], ("slot", "replaced"))
            self.assertEqual(roles[11], ("probe", "probe_no_share"))
            burst = db.execute(
                "SELECT judged_at FROM experiment_bursts WHERE "
                "burst_id=?", (burst_id,)).fetchone()
            self.assertIsNotNone(burst["judged_at"])
            event = db.execute(
                "SELECT detail_json FROM experiment_events WHERE "
                "kind='burst_judged'").fetchone()
            detail = json.loads(event["detail_json"])
            self.assertEqual(detail["verdict"], "promoted")
            self.assertEqual(detail["promote"], 10)
        finally:
            db.close()

    def test_judge_recovered_original_competes_and_wins(self):
        # the incumbent produced during the burst window: it is judged
        # on the same it/s vs it/s/$ footing and beats a weak probe
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [])
            self._dry_slot(db, phase)
            burst_id = add_burst(db, phase["phase_id"], 6,
                                 trigger="dry", trigger_order_id=1,
                                 n_target=3)
            add_obs(db, 1, 4, status(submitted=6, cpu=10, gpu=80))
            add_fleet(db, 10, 20)
            add_rental(db, phase["phase_id"], 10, 20, 8.5,
                       role="probe", burst_id=burst_id, hourly="0.15")
            add_obs(db, 10, 5, status(submitted=1, cpu=5, gpu=5))
            add_obs(db, 10, 0.5, status(submitted=2, cpu=5, gpu=5))
            acts, _ = actions(db, phase)
            judge = kinds(acts, "judge")[0]
            # probe its(10) falls below the floor vs original its(90)
            self.assertEqual(judge["verdict"], "kept_original")
            self.assertIsNone(judge["promote"])
            self.assertIsNone(judge["cancel_original"])
            self.assertEqual([c["order_id"] for c in judge["cancel"]],
                             [10])
            self.assertEqual(judge["cancel"][0]["reason"], "probe_lost")
        finally:
            db.close()

    def test_judge_recovered_original_outbid_by_margin(self):
        # the incumbent recovered but a finder beats its its/$ by more
        # than the swap margin -- and clears the fleet-mean growth
        # bar, so it earns a seat alongside rather than replacing
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [])
            self._dry_slot(db, phase)
            burst_id = add_burst(db, phase["phase_id"], 6,
                                 trigger="dry", trigger_order_id=1,
                                 n_target=3)
            add_obs(db, 1, 4, status(submitted=6, cpu=10, gpu=80))
            add_fleet(db, 10, 20)
            add_rental(db, phase["phase_id"], 10, 20, 8.5,
                       role="probe", burst_id=burst_id, hourly="0.05")
            add_obs(db, 10, 5, status(submitted=1, cpu=50, gpu=80))
            add_obs(db, 10, 0.5, status(submitted=3, cpu=50, gpu=80))
            acts, _ = actions(db, phase)
            judge = kinds(acts, "judge")[0]
            # orig eff sqrt(90)/0.10=94.9; probe
            # sqrt(130)/(0.05+fee/6)=171 > 94.9*1.15 -- and
            # 171 > 94.9*1.25 fleet-mean growth bar
            self.assertEqual(judge["verdict"], "earned_seat")
            self.assertEqual(judge["promote"], 10)
            self.assertIsNone(judge["cancel_original"])
        finally:
            db.close()

    def test_judge_no_finders_drought_hold_then_retry(self):
        # a proven incumbent still hashing gets one reprieve when the
        # whole audition turns up silent -- the silence is crediting/
        # tree drought, not dead hardware; the next all-quiet verdict
        # delivers the kill
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [])
            self._dry_slot(db, phase)
            burst_id = add_burst(db, phase["phase_id"], 6,
                                 trigger="dry", trigger_order_id=1,
                                 n_target=3)
            add_fleet(db, 10, 20)
            add_rental(db, phase["phase_id"], 10, 20, 8.5,
                       role="probe", burst_id=burst_id)
            add_obs(db, 10, 5, status(submitted=0))
            add_obs(db, 10, 0.5, status(submitted=0))
            acts, _ = actions(db, phase)
            judge = kinds(acts, "judge")[0]
            self.assertEqual(judge["verdict"], "drought_hold")
            self.assertIsNone(judge["cancel_original"])
            self.assertIsNone(judge["retry"])
            self.assertEqual([c["order_id"] for c in judge["cancel"]],
                             [10])
            with mock.patch.object(
                    experiment.rent, "cancel_managed_order",
                    return_value=(0, {})):
                experiment.execute(
                    db, ":memory:", "/tmp/env", phase, CFG, [judge],
                    NOW, live=True, say=lambda *_a: None)
            # reprieve consumed: a second all-quiet audition kills
            # outright and re-arms the vacancy after the y arm
            burst_id = add_burst(db, phase["phase_id"], 6,
                                 trigger="dry", trigger_order_id=1,
                                 n_target=3)
            add_fleet(db, 12, 22)
            add_rental(db, phase["phase_id"], 12, 22, 8.5,
                       role="probe", burst_id=burst_id)
            add_obs(db, 12, 5, status(submitted=0))
            add_obs(db, 12, 0.5, status(submitted=0))
            acts, _ = actions(db, phase)
            # the hold's y arm is still cooling: no fresh dry
            # audition fires for the held order yet
            self.assertFalse(any(
                b["trigger"] == "dry" and b["trigger_order_id"] == 1
                for b in kinds(acts, "burst")))
            judge = kinds(acts, "judge")[0]
            self.assertEqual(judge["verdict"], "dry_retry")
            self.assertEqual(judge["cancel_original"]["reason"], "dry")
            self.assertEqual(judge["retry"]["for_order"], 1)
            self.assertEqual(judge["retry"]["y_min"], 15)
            # a proven box re-auditions after one measured tree
            # rotation and carries its warrant into the refill
            self.assertTrue(judge["retry"]["proven"])
            self.assertEqual(
                judge["retry"]["due_at"],
                (NOW + timedelta(minutes=20)).isoformat())
            with mock.patch.object(
                    experiment.rent, "cancel_managed_order",
                    return_value=(0, {})):
                experiment.execute(
                    db, ":memory:", "/tmp/env", phase, CFG, [judge],
                    NOW, live=True, say=lambda *_a: None)
            sched = db.execute(
                "SELECT detail_json FROM experiment_events WHERE "
                "kind='burst_scheduled'").fetchone()
            self.assertEqual(
                json.loads(sched["detail_json"])["for_order"], 1)
        finally:
            db.close()

    def test_retry_burst_fires_at_due_no_reservation(self):
        db = make_db()
        try:
            phase = add_phase(db)
            add_history(db, 20, fleet_order=900, its_cpu=200,
                        its_gpu=800)
            add_snapshot(db, [listing(server_id=20)])
            # one producing slot, one vacated slot with a retry pending
            add_fleet(db, 2, 12)
            add_rental(db, phase["phase_id"], 2, 12, 60)
            add_obs(db, 2, 0.5, status(submitted=5))
            add_event(db, phase["phase_id"], 30, "burst_scheduled",
                      order_id=1,
                      detail={"for_order": 1, "server_id": 11,
                              "tier": "other", "y_min": 15,
                              "due_at": (NOW + timedelta(
                                  minutes=5)).isoformat()})
            acts, _ = actions(db, phase)
            # not due yet: no burst, and nothing reserves the
            # vacancy -- the plain fill takes it
            self.assertEqual(kinds(acts, "burst"), [])
            self.assertEqual(
                [r["server_id"] for r in kinds(acts, "rent")], [20])
            add_event(db, phase["phase_id"], 0.5, "burst_scheduled",
                      order_id=1,
                      detail={"for_order": 1, "server_id": 11,
                              "tier": "other", "y_min": 15,
                              "due_at": iso(0.5)})
            acts, _ = actions(db, phase)
            bursts = kinds(acts, "burst")
            self.assertEqual(len(bursts), 1)
            self.assertEqual(bursts[0]["trigger"], "dry_retry")
            self.assertEqual(bursts[0]["trigger_order_id"], 1)
            self.assertEqual(bursts[0]["y_min"], 15)
            # a started burst clears the pending retry
            self.assertEqual(
                experiment.pending_bursts(db, phase["phase_id"]),
                [{"for_order": 1, "server_id": 11, "tier": "other",
                  "y_min": 15, "proven": None,
                  "due_at": NOW - timedelta(seconds=30)}])
            add_event(db, phase["phase_id"], 0.2, "burst_started",
                      order_id=1, detail={"burst_id": 1})
            self.assertEqual(
                experiment.pending_bursts(db, phase["phase_id"]), [])
            # a due retry auditions even with no vacancy left: the
            # fleet being full is the judge's problem, not the
            # emitter's
            db2 = make_db()
            try:
                phase2 = add_phase(db2)
                add_snapshot(db2, [listing(server_id=20)])
                for oid, sid in ((2, 12), (3, 13)):
                    add_fleet(db2, oid, sid)
                    add_rental(db2, phase2["phase_id"], oid, sid, 60)
                    add_obs(db2, oid, 0.5, status(submitted=5))
                add_event(db2, phase2["phase_id"], 1,
                          "burst_scheduled", order_id=1,
                          detail={"for_order": 1, "y_min": 15,
                                  "due_at": iso(0.5)})
                acts, _ = actions(db2, phase2)
                self.assertEqual(kinds(acts, "retry_cleared"), [])
                bursts = kinds(acts, "burst")
                self.assertEqual(len(bursts), 1)
                self.assertEqual(bursts[0]["trigger"], "dry_retry")
                self.assertEqual(bursts[0]["trigger_order_id"], 1)
            finally:
                db2.close()
        finally:
            db.close()

    def test_retry_burst_directs_proven_server_past_tried(self):
        # a proven vacancy's retry targets the dead box itself: the
        # warrant carries it past tried history the way a pin does
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [
                listing(server_id=11, price=2.4),
                listing(server_id=20, price=2.4),
            ])
            # s11 is tried: its earlier rental ended this phase
            add_fleet(db, 1, 11)
            add_rental(db, phase["phase_id"], 1, 11, 60)
            db.execute("UPDATE experiment_rentals SET ended_at=?,"
                       " end_reason='dry' WHERE order_id=1",
                       (iso(20),))
            db.commit()
            add_event(db, phase["phase_id"], 1, "burst_scheduled",
                      order_id=1,
                      detail={"for_order": 1, "server_id": 11,
                              "tier": "other", "y_min": 15,
                              "proven": True,
                              "due_at": iso(0.5)})
            acts, _ = actions(db, phase)
            bursts = kinds(acts, "burst")
            self.assertEqual(len(bursts), 1)
            self.assertEqual(bursts[0]["trigger"], "dry_retry")
            self.assertEqual(bursts[0]["server_id"], 11)
            experiment.execute(
                db, ":memory:", "/tmp/env", phase, CFG, bursts,
                NOW, live=True, say=lambda *_a: None)
            # the fill re-rents s11 as the directed pick: tried
            # history can't reject the retry's own warrant
            acts, _ = actions(db, phase)
            probes = [r for r in kinds(acts, "rent")
                      if r.get("role") == "probe"]
            self.assertEqual(probes[0]["server_id"], 11)
        finally:
            db.close()

    def test_retry_burst_reschedules_and_promotes(self):
        # an empty-slot retry that finds nothing re-arms itself; one
        # that finds a producer promotes it into the vacancy
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [])
            burst_id = add_burst(db, phase["phase_id"], 6,
                                 trigger="dry_retry", trigger_order_id=1,
                                 n_target=3)
            add_event(db, phase["phase_id"], 6, "burst_started",
                      order_id=1,
                      detail={"burst_id": burst_id,
                              "trigger": "dry_retry", "n": 3,
                              "y_min": 15})
            add_fleet(db, 10, 20)
            add_rental(db, phase["phase_id"], 10, 20, 8.5,
                       role="probe", burst_id=burst_id)
            add_obs(db, 10, 5, status(submitted=0))
            add_obs(db, 10, 0.5, status(submitted=0))
            acts, _ = actions(db, phase)
            judge = kinds(acts, "judge")[0]
            self.assertEqual(judge["verdict"], "retry_again")
            self.assertEqual(judge["retry"]["for_order"], 1)
            self.assertEqual(judge["retry"]["y_min"], 15)
            # a producing probe is promoted into the vacancy instead
            db.execute("DELETE FROM rig_observations")
            add_obs(db, 10, 5, status(submitted=1))
            add_obs(db, 10, 0.5, status(submitted=3))
            db.execute("UPDATE experiment_bursts SET judged_at=NULL")
            db.commit()
            acts, _ = actions(db, phase)
            judge = kinds(acts, "judge")[0]
            self.assertEqual(judge["verdict"], "promoted")
            self.assertEqual(judge["promote"], 10)
            self.assertIsNone(judge["retry"])
        finally:
            db.close()

    def test_dry_retry_full_fleet_displaces_weakest(self):
        # the retry audition produced a dominant finder but the fleet
        # is full: it promotes by displacing the weakest incumbent
        # through the upgrade margin
        db = make_db()
        try:
            cfg = dict(CFG, max_rentals=1)
            phase = add_phase(db, cfg=cfg)
            add_snapshot(db, [])
            self._producing_slot(db, phase)  # v(90)/$0.10 = 94.9
            burst_id = add_burst(db, phase["phase_id"], 6,
                                 trigger="dry_retry",
                                 trigger_order_id=9, n_target=3)
            add_fleet(db, 10, 20)
            add_rental(db, phase["phase_id"], 10, 20, 8.5,
                       role="probe", burst_id=burst_id, hourly="0.05")
            add_obs(db, 10, 5, status(submitted=1, cpu=50, gpu=120))
            add_obs(db, 10, 0.5, status(submitted=6, cpu=50, gpu=120))
            acts, _ = actions(db, phase, cfg=cfg)
            judge = kinds(acts, "judge")[0]
            # probe v(170)/($0.05+fee/6) = 195.6 > 94.9 * 1.25
            self.assertEqual(judge["verdict"], "displaced")
            self.assertEqual(judge["promote"], 10)
            self.assertEqual(judge["cancel"][0]["order_id"], 1)
            self.assertEqual(judge["cancel"][0]["reason"], "displaced")
            self.assertIsNone(judge["retry"])
            stats = judge["stats"]
            self.assertEqual(stats["orig_its"], 90)
            self.assertEqual(stats["probe_its"], 170)
            self.assertEqual(stats["metric"], "value_per_usd")
            with mock.patch.object(
                    experiment.rent, "cancel_managed_order",
                    return_value=(0, {})):
                experiment.execute(
                    db, ":memory:", "/tmp/env", phase, cfg, [judge],
                    NOW, live=True, say=lambda *_a: None)
            roles = {r["order_id"]: (r["role"], r["end_reason"])
                     for r in db.execute(
                         "SELECT order_id, role, end_reason FROM "
                         "experiment_rentals")}
            self.assertEqual(roles[10], ("slot", None))
            self.assertEqual(roles[1], ("slot", "displaced"))
        finally:
            db.close()

    def test_dry_retry_full_fleet_strong_incumbent_no_vacancy(self):
        # same audition, but the weakest incumbent stands: the finder
        # loses, every probe is dropped, and with no vacancy the
        # retry chain dies
        db = make_db()
        try:
            cfg = dict(CFG, max_rentals=1)
            phase = add_phase(db, cfg=cfg)
            add_snapshot(db, [])
            self._producing_slot(db, phase, hourly="0.02")
            # orig v(90)/$0.02 = 474.4 -> margin 593 > probe 195.6
            burst_id = add_burst(db, phase["phase_id"], 6,
                                 trigger="dry_retry",
                                 trigger_order_id=9, n_target=3)
            add_fleet(db, 10, 20)
            add_rental(db, phase["phase_id"], 10, 20, 8.5,
                       role="probe", burst_id=burst_id, hourly="0.05")
            add_obs(db, 10, 5, status(submitted=1, cpu=50, gpu=120))
            add_obs(db, 10, 0.5, status(submitted=6, cpu=50, gpu=120))
            acts, _ = actions(db, phase, cfg=cfg)
            judge = kinds(acts, "judge")[0]
            self.assertEqual(judge["verdict"], "no_vacancy")
            self.assertIsNone(judge["promote"])
            self.assertIsNone(judge["retry"])
            self.assertEqual(
                [c["order_id"] for c in judge["cancel"]], [10])
            self.assertEqual(judge["cancel"][0]["reason"],
                             "probe_lost")
        finally:
            db.close()

    def _probe(self, db, phase, burst_id, order_id, server_id,
               cpu, gpu, hourly="0.05", created_min=8.5):
        """A live finder probe: produced shares during its audition."""
        add_fleet(db, order_id, server_id)
        add_rental(db, phase["phase_id"], order_id, server_id,
                   created_min, role="probe", burst_id=burst_id,
                   hourly=hourly)
        add_obs(db, order_id, 5, status(submitted=1, cpu=cpu, gpu=gpu))
        add_obs(db, order_id, 0.5,
                status(submitted=6, cpu=cpu, gpu=gpu))

    def test_judge_keeps_every_beat_displacing_weakest(self):
        # two probes each out-earn the weakest slot by the upgrade
        # margin: both keep their seats -- the weakest incumbents go
        # instead of throwing the second find away
        db = make_db()
        try:
            cfg = dict(CFG, max_rentals=2)
            phase = add_phase(db, cfg=cfg)
            add_snapshot(db, [])
            self._producing_slot(db, phase)             # o1 ~94.9
            self._producing_slot(db, phase, order_id=2, server_id=12)
            burst_id = add_burst(db, phase["phase_id"], 6,
                                 trigger="dry_retry",
                                 trigger_order_id=9, n_target=3)
            self._probe(db, phase, burst_id, 10, 20, 50, 120)  # 195.6
            self._probe(db, phase, burst_id, 11, 21, 50, 90)   # 177.5
            acts, _ = actions(db, phase, cfg=cfg)
            judge = kinds(acts, "judge")[0]
            self.assertEqual(judge["verdict"], "displaced")
            self.assertEqual(judge["promote"], 10)
            self.assertEqual(judge["promote_extra"], [
                {"order_id": 11, "server_id": 21, "tier": "other",
                 "via": "displace"}])
            displaced = {c["order_id"] for c in judge["cancel"]
                         if c["reason"] == "displaced"}
            self.assertEqual(displaced, {1, 2})
            with mock.patch.object(
                    experiment.rent, "cancel_managed_order",
                    return_value=(0, {})):
                experiment.execute(
                    db, ":memory:", "/tmp/env", phase, cfg, [judge],
                    NOW, live=True, say=lambda *_a: None)
            roles = {r["order_id"]: (r["role"], r["end_reason"])
                     for r in db.execute(
                         "SELECT order_id, role, end_reason FROM "
                         "experiment_rentals")}
            self.assertEqual(roles[10], ("slot", None))
            self.assertEqual(roles[11], ("slot", None))
            self.assertEqual(roles[1], ("slot", "displaced"))
            self.assertEqual(roles[2], ("slot", "displaced"))
        finally:
            db.close()

    def test_judge_extra_grows_above_weakest_seat(self):
        # a uniform fleet earns no mandate (target 1 < 2 seated) and
        # the best probe can't clear the displace bar on the weakest
        # incumbent -- but it beats the weakest seat, so it earns one
        # in the headroom under max_rentals. The next-best finder also
        # beats the weakest seat and grows too; only the probe below
        # every seat dies
        db = make_db()
        try:
            cfg = dict(CFG, slot_min=1, max_rentals=4,
                       growth_margin=0.10)
            phase = add_phase(db, cfg=cfg)
            add_snapshot(db, [])
            for oid, sid in ((1, 11), (2, 12)):
                add_fleet(db, oid, sid)
                add_rental(db, phase["phase_id"], oid, sid, 60,
                           hourly="0.10")
                add_obs(db, oid, 1,
                        status(submitted=10, cpu=45, gpu=180))
                # its=225 -> v/0.10 = 150 each
            burst_id = add_burst(db, phase["phase_id"], 6,
                                 trigger="dry_retry",
                                 trigger_order_id=9, n_target=3)
            self._probe(db, phase, burst_id, 10, 20, 50, 120)  # 173.8
            self._probe(db, phase, burst_id, 11, 21, 50, 90)   # 157.7
            self._probe(db, phase, burst_id, 12, 22, 10, 20)   # 73.0
            acts, _ = actions(db, phase, cfg=cfg)
            judge = kinds(acts, "judge")[0]
            # the best finder clears the weakest-seat bar -- 173.8 >
            # 150 -- so the headline verdict seats it alongside the
            # fleet rather than leaving the sweep to pick it up
            self.assertEqual(judge["verdict"], "earned_seat")
            self.assertEqual(judge["promote"], 10)
            self.assertIsNone(judge["cancel_original"])
            # 157.7 also outranks the weakest seat (150) and grows via
            # the extras path; 73.0 is below every seat and dies
            self.assertEqual(
                judge["promote_extra"],
                [{"order_id": 11, "server_id": 21, "tier": "other",
                  "via": "growth"}])
            drops = {c["order_id"]: c["reason"]
                     for c in judge["cancel"]}
            self.assertEqual(drops, {12: "probe_lost"})
        finally:
            db.close()

    def test_judge_kept_original_still_keeps_beats(self):
        # an upgrade audition misses its strong target, but a probe
        # that beats the OTHER weakest slot isn't thrown away
        db = make_db()
        try:
            cfg = dict(CFG, max_rentals=2)
            phase = add_phase(db, cfg=cfg)
            add_snapshot(db, [])
            self._producing_slot(db, phase, hourly="0.02")   # o1 474
            self._producing_slot(db, phase, order_id=2,
                                 server_id=12)               # o2 94.9
            burst_id = add_burst(db, phase["phase_id"], 6,
                                 trigger="upgrade",
                                 trigger_order_id=1, n_target=3)
            self._probe(db, phase, burst_id, 10, 20, 50, 120)  # 195.6
            self._probe(db, phase, burst_id, 11, 21, 50, 90)   # 177.5
            acts, _ = actions(db, phase, cfg=cfg)
            judge = kinds(acts, "judge")[0]
            # 195.6 < 474*1.25: the target keeps its slot...
            self.assertEqual(judge["verdict"], "kept_original")
            self.assertIsNone(judge["promote"])
            # ...but the best probe displaces weak o2; the second is
            # below the new average and has no seat left to take
            self.assertEqual(judge["promote_extra"], [
                {"order_id": 10, "server_id": 20, "tier": "other",
                 "via": "displace"}])
            cancels = {c["order_id"]: c["reason"]
                       for c in judge["cancel"]}
            self.assertEqual(cancels, {2: "displaced",
                                       11: "probe_lost"})
        finally:
            db.close()

    def test_succession_arms_early_for_spot_probes(self):
        # a 20-min window only fits an on-demand audition; when spot
        # probes are possible the burst arms fill+warmup+judge-window
        # earlier so the audition completes before the term ends
        db = make_db()
        try:
            cfg = dict(CFG, spot_enabled=True)
            phase = add_phase(db, cfg=cfg)
            add_snapshot(db, [])
            add_fleet(db, 1, 11, deadline_min=-30)  # term ends in 30m
            add_rental(db, phase["phase_id"], 1, 11, 180)
            add_obs(db, 1, 0.5, status(submitted=5))
            acts, _ = actions(db, phase, cfg=cfg)
            bursts = [a for a in kinds(acts, "burst")
                      if a["trigger"] == "succession"]
            self.assertEqual(len(bursts), 1)
            self.assertEqual(bursts[0]["trigger_order_id"], 1)
            # without spot the same slot sits outside the 20-min window
            acts, _ = actions(db, phase)
            self.assertFalse(any(
                a.get("trigger") == "succession" for a in acts))
        finally:
            db.close()

    def test_concurrent_succession_per_expiring_slot(self):
        # every expiring slot gets its own audition in the same cycle
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [])
            add_fleet(db, 1, 11, deadline_min=-15)
            add_rental(db, phase["phase_id"], 1, 11, 180)
            add_obs(db, 1, 0.5, status(submitted=5))
            add_fleet(db, 2, 12, deadline_min=-18)
            add_rental(db, phase["phase_id"], 2, 12, 180)
            add_obs(db, 2, 0.5, status(submitted=5))
            acts, _ = actions(db, phase)
            bursts = [a for a in kinds(acts, "burst")
                      if a["trigger"] == "succession"]
            self.assertEqual(
                sorted(a["trigger_order_id"] for a in bursts), [1, 2])
        finally:
            db.close()

    def test_concurrent_bursts_each_fill_own_probes(self):
        # two unjudged bursts drip a probe each, tagged to their own
        # burst_id -- the shared rental cap still binds both
        db = make_db()
        try:
            phase = add_phase(db)
            add_fleet(db, 1, 11, deadline_min=-15)
            add_rental(db, phase["phase_id"], 1, 11, 180)
            add_obs(db, 1, 0.5, status(submitted=5))
            add_fleet(db, 2, 12, deadline_min=-15)
            add_rental(db, phase["phase_id"], 2, 12, 180)
            add_obs(db, 2, 0.5, status(submitted=5))
            a = self._server_with_history(
                db, 20, "AMD Ryzen 9 9950X 16-Core Processor",
                "RTX 4090")
            b = self._server_with_history(
                db, 21, "AMD Ryzen 9 9950X 16-Core Processor",
                "RTX 4090", fleet_order=997)
            add_snapshot(db, [a, b])
            b1 = add_burst(db, phase["phase_id"], 0,
                           trigger="succession", trigger_order_id=1)
            b2 = add_burst(db, phase["phase_id"], 0,
                           trigger="succession", trigger_order_id=2)
            acts, _ = actions(db, phase)
            rents = [a for a in kinds(acts, "rent")
                     if a.get("role") == "probe"]
            self.assertEqual(
                sorted(a["burst_id"] for a in rents), [b1, b2])
            self.assertEqual(len({a["server_id"] for a in rents}), 2)
        finally:
            db.close()

    def test_one_judge_per_cycle_fifo(self):
        # two mature bursts: the older judges this cycle, the younger
        # waits for the next pass so vacancy math stays honest
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [])
            b1 = add_burst(db, phase["phase_id"], 10)
            add_burst(db, phase["phase_id"], 9)
            acts, _ = actions(db, phase)
            judges = kinds(acts, "judge")
            self.assertEqual(len(judges), 1)
            self.assertEqual(judges[0]["burst_id"], b1)
        finally:
            db.close()

    def _producing_slot(self, db, phase, order_id=1, server_id=11,
                        hourly="0.10", submitted=10):
        add_fleet(db, order_id, server_id)
        add_rental(db, phase["phase_id"], order_id, server_id, 60,
                   hourly=hourly)
        add_obs(db, order_id, 1, status(submitted=submitted))

    def _server_with_history(self, db, server_id, cpu, gpu,
                             fleet_order=998, its_cpu=200, its_gpu=800):
        return add_history(db, server_id, cpu, gpu,
                           fleet_order=fleet_order, its_cpu=its_cpu,
                           its_gpu=its_gpu)

    def test_upgrade_trigger_needs_predicted_dominance(self):
        db = make_db()
        try:
            cfg = dict(CFG, max_rentals=1)
            phase = add_phase(db, cfg=cfg)
            self._producing_slot(db, phase)  # v(90)/$0.10 = 94.9 v/$
            strong = self._server_with_history(
                db, 20, "AMD Ryzen 9 9950X 16-Core Processor",
                "RTX 4090")  # history its ~1000
            strong["price"] = {"usd": {"on_demand_usd": 0.5,
                                       "spot": 0}}  # $0.021/h
            add_snapshot(db, [strong])
            acts, state = actions(db, phase, cfg=cfg)
            # est v(1000)/(0.0219+fee) ~ 820 > 94.9 * 1.25: fires
            bursts = kinds(acts, "burst")
            self.assertEqual(len(bursts), 1)
            self.assertEqual(bursts[0]["trigger"], "upgrade")
            self.assertEqual(bursts[0]["trigger_order_id"], 1)
            self.assertEqual(bursts[0]["server_id"], 20)
            self.assertEqual(bursts[0]["n"], 1)
            # a listing that merely matches on it/s does not qualify
            db2 = make_db()
            try:
                phase2 = add_phase(db2, cfg=cfg)
                self._producing_slot(db2, phase2)
                weak = self._server_with_history(
                    db2, 20, "Intel Core i9-10980XE", "RTX 3070",
                    fleet_order=997, its_cpu=10,
                    its_gpu=80)  # same its as the slot
                add_snapshot(db2, [weak])
                acts2, _ = actions(db2, phase2, cfg=cfg)
                # v(90)/(0.105+fee) ~ 78 < 94.9 * 1.25: no dominance
                self.assertEqual(kinds(acts2, "burst"), [])
            finally:
                db2.close()
            # a vacancy means the fill captures the upside for free
            db.execute(
                "UPDATE experiment_rentals SET ended_at=?,"
                " end_reason='term_end' WHERE order_id=1", (iso(10),))
            db.commit()
            acts, _ = actions(db, phase, cfg=cfg)
            self.assertEqual(kinds(acts, "burst"), [])
            self.assertEqual(kinds(acts, "rent")[0]["server_id"], 20)
            # already-tried servers are not re-auditioned
            db.execute(
                "UPDATE experiment_rentals SET ended_at=NULL,"
                " end_reason=NULL WHERE order_id=1")
            add_rental(db, phase["phase_id"], 9, 20, 400, reason="fill")
            db.execute(
                "UPDATE experiment_rentals SET ended_at=?,"
                " end_reason='dry' WHERE order_id=9", (iso(300),))
            db.commit()
            acts, _ = actions(db, phase, cfg=cfg)
            self.assertEqual(kinds(acts, "burst"), [])
        finally:
            db.close()

    def test_upgrade_trigger_uses_value_per_usd(self):
        # the weakest slot is picked by value/$: B hashes harder per
        # dollar than A (v(100)/$0.02 = 500 vs v(1000)/$0.2835 =
        # 111.5), so A rides out the audition. A challenger predicted
        # at v(1500)/($0.21+fee) = 170.9 clears A * 1.25: fires.
        db = make_db()
        try:
            cfg = dict(CFG, max_rentals=2)
            phase = add_phase(db, cfg=cfg)
            add_fleet(db, 1, 11)
            add_rental(db, phase["phase_id"], 1, 11, 60,
                       hourly="0.2835")
            add_obs(db, 1, 1, status(submitted=1, cpu=200, gpu=800))
            add_fleet(db, 2, 12)
            add_rental(db, phase["phase_id"], 2, 12, 60,
                       hourly="0.02")
            add_obs(db, 2, 1, status(submitted=10, cpu=20, gpu=80))
            chal = self._server_with_history(
                db, 20, "AMD Ryzen 9 9950X 16-Core Processor",
                "RTX 4090", its_cpu=300, its_gpu=1200)  # est 1500 its
            # on_demand_usd is a daily price: $4.80/day = $0.21/h
            chal["price"] = {"usd": {"on_demand_usd": 4.8, "spot": 0}}
            add_snapshot(db, [chal])
            acts, _ = actions(db, phase, cfg=cfg)
            bursts = kinds(acts, "burst")
            self.assertEqual(len(bursts), 1)
            self.assertEqual(bursts[0]["trigger"], "upgrade")
            self.assertEqual(bursts[0]["trigger_order_id"], 1)
            self.assertEqual(bursts[0]["server_id"], 20)
        finally:
            db.close()

    def test_upgrade_trigger_fee_blocks_marginal_swap(self):
        # the creation fee is part of the challenger's eff_hr.
        # Incumbent v(1000)/$0.2835 = 111.5, need >139.4. Challenger
        # est 1100 -> v=33.17: @ $0.24/h it misses
        # (33.17/(0.24+0.0167) = 129.2 < 139.4); @ $0.20/h it clears
        # (33.17/0.2167 = 153.1).
        db = make_db()
        try:
            cfg = dict(CFG, max_rentals=1)
            phase = add_phase(db, cfg=cfg)
            add_fleet(db, 1, 11)
            add_rental(db, phase["phase_id"], 1, 11, 60,
                       hourly="0.2835")
            add_obs(db, 1, 1, status(submitted=1, cpu=200, gpu=800))
            chal = self._server_with_history(
                db, 20, "AMD Ryzen 9 9950X 16-Core Processor",
                "RTX 4090", its_cpu=200, its_gpu=900)  # est 1100 its
            chal["price"] = {"usd": {"on_demand_usd": 5.76,
                                     "spot": 0}}  # $0.24/h
            add_snapshot(db, [chal])
            acts, _ = actions(db, phase, cfg=cfg)
            self.assertEqual(kinds(acts, "burst"), [])

            db2 = make_db()
            try:
                phase2 = add_phase(db2, cfg=cfg)
                add_fleet(db2, 1, 11)
                add_rental(db2, phase2["phase_id"], 1, 11, 60,
                           hourly="0.2835")
                add_obs(db2, 1, 1,
                        status(submitted=1, cpu=200, gpu=800))
                chal2 = self._server_with_history(
                    db2, 20, "AMD Ryzen 9 9950X 16-Core Processor",
                    "RTX 4090", its_cpu=200, its_gpu=900)
                chal2["price"] = {"usd": {"on_demand_usd": 4.8,
                                          "spot": 0}}  # $0.20/h
                add_snapshot(db2, [chal2])
                acts2, _ = actions(db2, phase2, cfg=cfg)
                self.assertEqual(kinds(acts2, "burst")[0]["trigger"],
                                 "upgrade")
            finally:
                db2.close()
        finally:
            db.close()

    def _vacancy_blocked_fixture(self, budget="10.50"):
        """One producing incumbent ($0.144375/h, 90 its) with a
        second slot vacant: ~$0.025/h headroom, so no normal fill
        fits and only a replacement audition can use the vacancy."""
        db = make_db()
        cfg = dict(CFG, max_rentals=2)
        phase = add_phase(db, budget=budget, reserve="0.50", cfg=cfg)
        self._producing_slot(db, phase, order_id=1, server_id=11,
                             hourly="0.144375")
        return db, phase, cfg

    def _challenger(self, db, server_id=20, price_day=1.2,
                    its_cpu=20, its_gpu=80):
        doc = self._server_with_history(
            db, server_id, "AMD Ryzen 9 9950X 16-Core Processor",
            "RTX 4090", fleet_order=9000 + server_id,
            its_cpu=its_cpu, its_gpu=its_gpu)
        doc["price"] = {"usd": {"on_demand_usd": price_day,
                                "spot": 0}}
        return doc

    def test_vacancy_blocked_upgrade_audition(self):
        # incumbent v(90)/$0.144 = 65.7; challenger est 100 its at
        # $0.0525/h -> eff $0.0692/h, v/$ prior 144.5 > 82.1 margin,
        # cheaper than the incumbent and inside the replacement pace
        # cap -- but over the $0.025/h headroom for a plain fill.
        # slot_min=1 keeps the fleet at its minimum (below it the
        # measured challenger would take the exemption and fill
        # outright instead of auditioning); static keeps slot_target
        # at max_rentals so the vacancy still exists to be blocked
        db, phase, cfg = self._vacancy_blocked_fixture()
        cfg = dict(cfg, slot_min=1, slot_mode="static")
        try:
            add_snapshot(db, [self._challenger(db)])
            notes = []
            acts, _ = actions(db, phase, cfg=cfg, say=notes.append)
            bursts = kinds(acts, "burst")
            self.assertEqual(len(bursts), 1)
            self.assertEqual(bursts[0]["trigger"], "upgrade")
            self.assertEqual(bursts[0]["trigger_order_id"], 1)
            self.assertEqual(bursts[0]["server_id"], 20)
            self.assertEqual(bursts[0]["n"], 1)
            # an audition only: no incumbent cancel, no slot rent
            self.assertFalse(
                any(c["order_id"] == 1
                    for c in kinds(acts, "cancel")))
            self.assertEqual(kinds(acts, "rent"), [])
            self.assertTrue(any(
                "vacant slot blocked by pace/budget" in n
                and "audition s20" in n and "vs #1" in n
                for n in notes))
            # identical outcome through the dollar-forecast path:
            # stub usd_hr at $0.30/h -> ret 4.3x, margin 2.6x
            state = experiment.build_state(
                db, phase, NOW, cfg, say=lambda *_a: None)
            state["value_model"]["usd_hr"] = lambda its: 0.30
            acts = experiment.decide(NOW, state, cfg,
                                     say=lambda *_a: None)
            bursts = kinds(acts, "burst")
            self.assertEqual(len(bursts), 1)
            self.assertEqual(bursts[0]["server_id"], 20)
            self.assertEqual(bursts[0]["trigger_order_id"], 1)
        finally:
            db.close()

    def test_vacancy_normal_fill_preferred_over_audition(self):
        # same challenger, healthy budget: the plain fill fits and
        # the upgrade route stays off
        db, phase, cfg = self._vacancy_blocked_fixture(
            budget="100.00")
        try:
            add_snapshot(db, [self._challenger(db)])
            acts, _ = actions(db, phase, cfg=cfg)
            self.assertEqual(kinds(acts, "burst"), [])
            rents = kinds(acts, "rent")
            self.assertEqual([r["server_id"] for r in rents], [20])
            self.assertEqual(rents[0]["reason"], "fill")
        finally:
            db.close()

    def test_vacancy_blocked_upgrade_gate_failures(self):
        # challenger predicted below the incumbent's margin
        db, phase, cfg = self._vacancy_blocked_fixture()
        try:
            add_snapshot(db, [self._challenger(db, its_cpu=1,
                                               its_gpu=1)])
            acts, _ = actions(db, phase, cfg=cfg)
            self.assertEqual(kinds(acts, "burst"), [])
        finally:
            db.close()
        # dominant but not cheaper than the incumbent burn it frees:
        # $3.0/day -> eff $0.148/h > $0.144375/h (pace cap $0.169/h
        # would have admitted it, the cheap test refuses)
        db, phase, cfg = self._vacancy_blocked_fixture()
        try:
            add_snapshot(db, [self._challenger(
                db, price_day=3.0, its_cpu=200, its_gpu=800)])
            acts, _ = actions(db, phase, cfg=cfg)
            self.assertEqual(kinds(acts, "burst"), [])
        finally:
            db.close()
        # below the relative floor: a dominant listing in cooldown
        # (prior 457) sets the floor at 319, above challenger's 144
        db, phase, cfg = self._vacancy_blocked_fixture()
        try:
            dominant = self._challenger(
                db, server_id=21, its_cpu=200, its_gpu=800)
            add_snapshot(db, [self._challenger(db), dominant])
            add_event(db, phase["phase_id"], 30, "burst_started",
                      detail={"burst_id": 77, "trigger": "upgrade",
                              "server_id": 21})
            add_event(db, phase["phase_id"], 25, "burst_judged",
                      detail={"burst_id": 77,
                              "verdict": "kept_original"})
            acts, _ = actions(db, phase, cfg=cfg)
            self.assertEqual(kinds(acts, "burst"), [])
        finally:
            db.close()
        # below min_return under the model: ret 0.72x beats the
        # 0.43x margin but fails the 1.2x absolute gate
        db, phase, cfg = self._vacancy_blocked_fixture()
        try:
            add_snapshot(db, [self._challenger(db)])
            state = experiment.build_state(
                db, phase, NOW, cfg, say=lambda *_a: None)
            state["value_model"]["usd_hr"] = lambda its: 0.05
            acts = experiment.decide(NOW, state, cfg,
                                     say=lambda *_a: None)
            self.assertEqual(kinds(acts, "burst"), [])
        finally:
            db.close()
        # the quote bound exceeds the remaining budget: $0.52 needed,
        # $0.20 left -- the audition would be dead on arrival
        db, phase, cfg = self._vacancy_blocked_fixture(budget="1.40")
        try:
            add_snapshot(db, [self._challenger(db)])
            acts, _ = actions(db, phase, cfg=cfg)
            self.assertEqual(kinds(acts, "burst"), [])
        finally:
            db.close()
        # over the replacement pace cap as well as the incumbent's
        # own hourly (headroom > 0 makes the cheap test bind first)
        db, phase, cfg = self._vacancy_blocked_fixture()
        try:
            add_snapshot(db, [self._challenger(
                db, price_day=4.2, its_cpu=200, its_gpu=800)])
            acts, _ = actions(db, phase, cfg=cfg)
            self.assertEqual(kinds(acts, "burst"), [])
        finally:
            db.close()

    def test_vacancy_blocked_upgrade_suppressed(self):
        # a pending rerent reserves the vacancy: no replacement scan
        db, phase, cfg = self._vacancy_blocked_fixture()
        try:
            add_snapshot(db, [self._challenger(db)])
            add_event(db, phase["phase_id"], 10, "rerent_scheduled",
                      server_id=99,
                      detail={"server_id": 99,
                              "due_at": (NOW + timedelta(
                                  minutes=30)).isoformat()})
            acts, _ = actions(db, phase, cfg=cfg)
            self.assertEqual(kinds(acts, "burst"), [])
        finally:
            db.close()
        # a pending retry burst holds nothing: it is a pure audition,
        # so the replacement scan still runs
        db, phase, cfg = self._vacancy_blocked_fixture()
        try:
            add_snapshot(db, [self._challenger(db)])
            add_event(db, phase["phase_id"], 10, "burst_scheduled",
                      detail={"for_order": 1, "y_min": 15,
                              "due_at": (NOW + timedelta(
                                  minutes=30)).isoformat()})
            acts, _ = actions(db, phase, cfg=cfg)
            bursts = kinds(acts, "burst")
            self.assertEqual(len(bursts), 1)
            self.assertEqual(bursts[0]["trigger"], "upgrade")
        finally:
            db.close()
        # the challenger's own failed audition this hour is still
        # cooling down
        db, phase, cfg = self._vacancy_blocked_fixture()
        try:
            add_snapshot(db, [self._challenger(db)])
            add_event(db, phase["phase_id"], 30, "burst_started",
                      detail={"burst_id": 77, "trigger": "upgrade",
                              "server_id": 20})
            add_event(db, phase["phase_id"], 25, "burst_judged",
                      detail={"burst_id": 77,
                              "verdict": "kept_original"})
            acts, _ = actions(db, phase, cfg=cfg)
            self.assertEqual(kinds(acts, "burst"), [])
        finally:
            db.close()

    def test_upgrade_judge_dominance_swap(self):
        # the finder's measured value/$ beats the incumbent's by the
        # margin (fee amortized on the probe, sunk on the incumbent):
        # it is promoted and the incumbent is 'upgraded' out. The
        # fleet is at max_rentals capacity, so the dominant winner
        # must swap -- it cannot earn a new seat
        db = make_db()
        try:
            cfg = dict(CFG, max_rentals=1)
            phase = add_phase(db, cfg=cfg)
            add_snapshot(db, [])
            self._producing_slot(db, phase, hourly="0.10", submitted=2)
            burst_id = add_burst(db, phase["phase_id"], 6,
                                 trigger="upgrade", trigger_order_id=1,
                                 n_target=1)
            add_obs(db, 1, 4, status(submitted=1, cpu=10, gpu=80))
            add_fleet(db, 10, 20)
            add_rental(db, phase["phase_id"], 10, 20, 8.5,
                       role="probe", burst_id=burst_id, hourly="0.05")
            add_obs(db, 10, 5, status(submitted=1, cpu=50, gpu=120))
            add_obs(db, 10, 0.5, status(submitted=6, cpu=50, gpu=120))
            acts, _ = actions(db, phase, cfg=cfg)
            judge = kinds(acts, "judge")[0]
            self.assertEqual(judge["verdict"], "upgraded")
            self.assertEqual(judge["promote"], 10)
            self.assertEqual(judge["cancel_original"]["reason"],
                             "upgraded")
            stats = judge["stats"]
            self.assertGreater(stats["probe_value_per_usd"],
                               stats["orig_value_per_usd"])
            self.assertEqual(stats["orig_its"], 90)
            self.assertEqual(stats["probe_its"], 170)
            with mock.patch.object(
                    experiment.rent, "cancel_managed_order",
                    return_value=(0, {})):
                experiment.execute(
                    db, ":memory:", "/tmp/env", phase, CFG, [judge],
                    NOW, live=True, say=lambda *_a: None)
            roles = {r["order_id"]: (r["role"], r["end_reason"])
                     for r in db.execute(
                         "SELECT order_id, role, end_reason FROM "
                         "experiment_rentals")}
            self.assertEqual(roles[10], ("slot", None))
            self.assertEqual(roles[1], ("slot", "upgraded"))
        finally:
            db.close()

    def test_upgrade_judge_below_margin_keeps_original(self):
        # v(15)/($0.02+fee/6) = 105.6 < v(90)/$0.10 * 1.25 = 118.6:
        # the incumbent stays, only the probe is cancelled
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [])
            self._producing_slot(db, phase, hourly="0.10", submitted=2)
            burst_id = add_burst(db, phase["phase_id"], 6,
                                 trigger="upgrade", trigger_order_id=1,
                                 n_target=1)
            add_obs(db, 1, 4, status(submitted=1, cpu=10, gpu=80))
            add_fleet(db, 10, 20)
            add_rental(db, phase["phase_id"], 10, 20, 8.5,
                       role="probe", burst_id=burst_id, hourly="0.02")
            add_obs(db, 10, 5, status(submitted=1, cpu=5, gpu=10))
            add_obs(db, 10, 0.5, status(submitted=6, cpu=5, gpu=10))
            acts, _ = actions(db, phase)
            judge = kinds(acts, "judge")[0]
            self.assertEqual(judge["verdict"], "kept_original")
            self.assertIsNone(judge["promote"])
            self.assertIsNone(judge["cancel_original"])
            self.assertEqual([c["reason"] for c in judge["cancel"]],
                             ["probe_lost"])
        finally:
            db.close()

    def test_failed_upgrade_cools_challenger(self):
        """No daily cap on upgrades; instead a burst that judged
        without promoting cools its target for upgrade_cooldown_min.
        Otherwise the same dominant-but-unrentable listing would
        re-trigger an audition every burst window."""
        db = make_db()
        try:
            cfg = dict(CFG, max_rentals=1)
            phase = add_phase(db, cfg=cfg)
            self._producing_slot(db, phase)
            docs = []
            for sid, oid in ((20, 998), (21, 999)):
                doc = self._server_with_history(
                    db, sid, "AMD Ryzen 9 9950X 16-Core Processor",
                    "RTX 4090", fleet_order=oid)
                doc["price"] = {"usd": {"on_demand_usd": 0.5,
                                        "spot": 0}}
                docs.append(doc)
            # burst events only (no experiment_bursts row): an
            # already-finished audition, judged without promoting
            add_event(db, phase["phase_id"], 30, "burst_started",
                      detail={"burst_id": 99, "trigger": "upgrade",
                              "n": 1, "server_id": 20})
            add_event(db, phase["phase_id"], 25, "burst_judged",
                      detail={"burst_id": 99, "verdict":
                              "kept_original"})
            add_snapshot(db, docs)
            acts, _ = actions(db, phase, cfg=cfg)
            ups = [a for a in kinds(acts, "burst")
                   if a["trigger"] == "upgrade"]
            self.assertEqual(len(ups), 1)
            # s20 is cooled, so the scan aims at the next challenger
            self.assertEqual(ups[0]["server_id"], 21)
        finally:
            db.close()
        # an 'upgraded' verdict does not cool: the winner earned a
        # slot and is a tried_server anyway
        db = make_db()
        try:
            cfg = dict(CFG, max_rentals=1)
            phase = add_phase(db, cfg=cfg)
            self._producing_slot(db, phase)
            doc = self._server_with_history(
                db, 20, "AMD Ryzen 9 9950X 16-Core Processor",
                "RTX 4090")
            doc["price"] = {"usd": {"on_demand_usd": 0.5, "spot": 0}}
            add_snapshot(db, [doc])
            add_event(db, phase["phase_id"], 30, "burst_started",
                      detail={"burst_id": 98, "trigger": "upgrade",
                              "n": 1, "server_id": 20})
            add_event(db, phase["phase_id"], 25, "burst_judged",
                      detail={"burst_id": 98, "verdict": "upgraded"})
            acts, _ = actions(db, phase, cfg=cfg)
            ups = [a for a in kinds(acts, "burst")
                   if a["trigger"] == "upgrade"]
            self.assertEqual(len(ups), 1)
            self.assertEqual(ups[0]["server_id"], 20)
        finally:
            db.close()
        # and an expired cooldown (older than upgrade_cooldown_min)
        # lets the challenger audition again
        db = make_db()
        try:
            cfg = dict(CFG, max_rentals=1)
            phase = add_phase(db, cfg=cfg)
            self._producing_slot(db, phase)
            doc = self._server_with_history(
                db, 20, "AMD Ryzen 9 9950X 16-Core Processor",
                "RTX 4090")
            doc["price"] = {"usd": {"on_demand_usd": 0.5, "spot": 0}}
            add_snapshot(db, [doc])
            add_event(db, phase["phase_id"], 90, "burst_started",
                      detail={"burst_id": 97, "trigger": "upgrade",
                              "n": 1, "server_id": 20})
            add_event(db, phase["phase_id"], 85, "burst_judged",
                      detail={"burst_id": 97, "verdict":
                              "kept_original"})
            acts, _ = actions(db, phase, cfg=cfg)
            ups = [a for a in kinds(acts, "burst")
                   if a["trigger"] == "upgrade"]
            self.assertEqual(len(ups), 1)
            self.assertEqual(ups[0]["server_id"], 20)
        finally:
            db.close()

    def test_upgrade_burst_never_dry_cancels_incumbent(self):
        # an upgrade burst with a quiet incumbent and no finders must
        # keep the original -- never fall into the dry-cancel path
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [])
            self._dry_slot(db, phase)
            burst_id = add_burst(db, phase["phase_id"], 6,
                                 trigger="upgrade", trigger_order_id=1,
                                 n_target=1)
            add_fleet(db, 10, 20)
            add_rental(db, phase["phase_id"], 10, 20, 8.5,
                       role="probe", burst_id=burst_id)
            add_obs(db, 10, 5, status(submitted=0))
            add_obs(db, 10, 0.5, status(submitted=0))
            acts, _ = actions(db, phase)
            judge = kinds(acts, "judge")[0]
            self.assertEqual(judge["verdict"], "kept_original")
            self.assertIsNone(judge["promote"])
            self.assertIsNone(judge["cancel_original"])
            self.assertIsNone(judge["retry"])
            self.assertEqual([c["order_id"] for c in judge["cancel"]],
                             [10])
        finally:
            db.close()

    def test_upgrade_judge_promotes_on_value_per_usd(self):
        # the incumbent never produced during the burst; the finder is
        # promoted only when its measured value/$ beats the original's
        # by the margin -- creation fee amortized on the probe only.
        # Here the win also clears the fleet-mean growth bar, so it
        # earns a seat alongside instead of swapping
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [])
            self._dry_slot(db, phase)  # its 90, quiet since 40m ago
            burst_id = add_burst(db, phase["phase_id"], 6,
                                 trigger="upgrade", trigger_order_id=1,
                                 n_target=1)
            add_fleet(db, 10, 20)
            add_rental(db, phase["phase_id"], 10, 20, 8.5,
                       role="probe", burst_id=burst_id, hourly="0.05")
            add_obs(db, 10, 5, status(submitted=1, cpu=50, gpu=120))
            add_obs(db, 10, 0.5, status(submitted=6, cpu=50, gpu=120))
            acts, _ = actions(db, phase)
            judge = kinds(acts, "judge")[0]
            # orig v(90)/$0.10 = 94.9; probe
            # v(170)/($0.05+$0.10/6) = 195.6 > 94.9 * 1.25 -- and the
            # fleet mean is the lone incumbent, so the growth bar is
            # the same number: the probe earns a NEW seat
            self.assertEqual(judge["verdict"], "earned_seat")
            self.assertEqual(judge["promote"], 10)
            self.assertIsNone(judge["cancel_original"])
            stats = judge["stats"]
            self.assertEqual(stats["orig_its"], 90)
            self.assertEqual(stats["probe_its"], 170)
            self.assertAlmostEqual(stats["orig_value_per_usd"], 94.9,
                                   places=1)
            # the same probe priced higher falls below the margin:
            # v(170)/($0.15+$0.10/6) = 78.2 < 118.6
            db.execute(
                "UPDATE experiment_rentals SET hourly_usd='0.15' "
                "WHERE order_id=10")
            db.execute("UPDATE experiment_bursts SET judged_at=NULL")
            db.commit()
            acts, _ = actions(db, phase)
            judge = kinds(acts, "judge")[0]
            self.assertEqual(judge["verdict"], "kept_original")
            self.assertIsNone(judge["promote"])
            self.assertIsNone(judge["cancel_original"])
        finally:
            db.close()

    def test_judged_burst_rents_no_probe_same_cycle(self):
        # a burst judged this cycle must not also fill a probe for it:
        # the new probe would be an orphan the judge never saw
        db = make_db()
        try:
            cfg = dict(CFG, probe_judge_min=2)
            phase = add_phase(db, cfg=cfg)
            add_snapshot(db, [listing(server_id=20)])
            burst_id = add_burst(db, phase["phase_id"], 4, n_target=3)
            for oid, sid in ((10, 30), (11, 31)):
                add_fleet(db, oid, sid)
                add_rental(db, phase["phase_id"], oid, sid, 3,
                           role="probe", burst_id=burst_id)
                add_obs(db, oid, 3, status(submitted=0))
                add_obs(db, oid, 0.5, status(submitted=0))
            acts, _ = actions(db, phase, cfg=cfg)
            self.assertEqual(len(kinds(acts, "judge")), 1)
            self.assertEqual(
                [r for r in kinds(acts, "rent")
                 if r.get("role") == "probe"], [])
        finally:
            db.close()

    def test_judge_fires_when_finder_nears_deadline(self):
        # probes rent one per cycle and mature staggered: a proven
        # finder about to hit its free-window deadline must not wait
        # for stragglers -- judge now with whoever is live
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [])
            self._dry_slot(db, phase)
            burst_id = add_burst(db, phase["phase_id"], 8,
                                 trigger="dry", trigger_order_id=1,
                                 n_target=3)
            # finder at cancel_by-1 min: still live, nearly at its
            # deadline
            add_fleet(db, 10, 20)
            add_rental(db, phase["phase_id"], 10, 20,
                       CFG["probe_cancel_by_min"] - 1,
                       role="probe", burst_id=burst_id)
            add_obs(db, 10, 4, status(submitted=1))
            add_obs(db, 10, 0.5, status(submitted=3))
            # straggler: young and not yet matured
            add_fleet(db, 11, 21)
            add_rental(db, phase["phase_id"], 11, 21, 2,
                       role="probe", burst_id=burst_id)
            add_obs(db, 11, 1.5, status(submitted=0))
            acts, _ = actions(db, phase)
            judge = kinds(acts, "judge")[0]
            self.assertEqual(judge["verdict"], "promoted")
            self.assertEqual(judge["promote"], 10)
            self.assertEqual([c["order_id"] for c in judge["cancel"]],
                             [11])
        finally:
            db.close()

    def test_probe_pace_allowance_adds_incumbent_burn(self):
        # a probe auditions for the incumbent's slot: its pace
        # allowance is headroom + the incumbent's own hourly, so a
        # challenger that only fits by replacing that burn still goes
        db = make_db()
        try:
            cfg = dict(CFG, max_rentals=2)
            phase = add_phase(db, budget="10", cfg=cfg)
            add_snapshot(db, [listing(server_id=20, price=2.4),
                              listing(server_id=21, price=48.0)])
            add_fleet(db, 1, 11)
            add_rental(db, phase["phase_id"], 1, 11, 60,
                       hourly="0.30")
            add_obs(db, 1, 40, status(submitted=5))
            add_obs(db, 1, 0.5, status(submitted=5))
            add_burst(db, phase["phase_id"], 1, trigger="dry",
                      trigger_order_id=1, n_target=2)
            acts, _ = actions(db, phase, cfg=cfg)
            # headroom alone is negative (sustainable ~$0.19/h vs
            # $0.30/h burn); +incumbent gives ~$0.19/h: s20 eff
            # $0.12/h fits, s21 eff $2.12/h is over_pace
            probes = [r for r in kinds(acts, "rent")
                      if r.get("role") == "probe"]
            self.assertEqual(len(probes), 1)
            self.assertEqual(probes[0]["server_id"], 20)
        finally:
            db.close()

    def test_cancel_retry_inside_free_window_skips_backoff(self):
        # a failed cancel retries immediately while the order is still
        # inside the free window; past it the 10-min backoff applies
        db = make_db()
        try:
            phase = add_phase(db)
            add_fleet(db, 1, 11)
            add_rental(db, phase["phase_id"], 1, 11, 7)
            add_event(db, phase["phase_id"], 1, "cancel_failed",
                      order_id=1)
            with mock.patch.object(
                    experiment.rent, "cancel_managed_order",
                    return_value=(0, {})) as cancel_mock:
                experiment._exec_cancel(
                    db, ":memory:", "/unused", phase["phase_id"],
                    {"order_id": 1, "server_id": 11, "reason": "dry"},
                    NOW, lambda *_a: None, free_window_min=10)
            cancel_mock.assert_called_once()
            # order age 15min: outside the free window -> backoff
            db.execute("DELETE FROM experiment_events")
            db.execute(
                "UPDATE experiment_rentals SET created_at=?,"
                " ended_at=NULL, end_reason=NULL WHERE order_id=1",
                (iso(15),))
            db.commit()
            add_event(db, phase["phase_id"], 1, "cancel_failed",
                      order_id=1)
            with mock.patch.object(
                    experiment.rent, "cancel_managed_order",
                    return_value=(0, {})) as cancel_mock:
                experiment._exec_cancel(
                    db, ":memory:", "/unused", phase["phase_id"],
                    {"order_id": 1, "server_id": 11, "reason": "dry"},
                    NOW, lambda *_a: None, free_window_min=10)
            cancel_mock.assert_not_called()
            row = db.execute(
                "SELECT detail_json FROM experiment_events WHERE "
                "kind='blocked'").fetchone()
            self.assertEqual(
                json.loads(row["detail_json"])["reason"],
                "cancel_retry_backoff")
        finally:
            db.close()

    def test_past_deadline_probe_excluded_from_judge(self):
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [])
            burst_id = add_burst(db, phase["phase_id"], 10,
                                 n_target=3)
            add_fleet(db, 10, 20)
            # age 9.5 min: inside [cancel_by-1, cancel_by) maturity
            # band it would judge, but past deadline it must not be
            # kept or judged-cancelled.
            add_rental(db, phase["phase_id"], 10, 20, 9.5,
                       role="probe", burst_id=burst_id)
            add_obs(db, 10, 6, status(submitted=1))
            add_obs(db, 10, 0.5, status(submitted=2))
            acts, _ = actions(db, phase)
            cancels = kinds(acts, "cancel")
            self.assertEqual([c["reason"] for c in cancels],
                             ["probe_deadline"])
            judges = kinds(acts, "judge")
            self.assertEqual(len(judges), 1)
            self.assertNotEqual(judges[0]["promote"], 10)
            self.assertNotIn(10, [c["order_id"]
                                  for c in judges[0]["cancel"]])
        finally:
            db.close()

    def test_probe_guard_blocks_rents(self):
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [listing(server_id=20)])
            burst_id = add_burst(db, phase["phase_id"], 1, n_target=3)
            add_fleet(db, 10, 30)
            # 5.5 min is past cancel_by(9) - PROBE_RENT_GUARD_MIN(4)
            add_rental(db, phase["phase_id"], 10, 30, 5.5,
                       role="probe", burst_id=burst_id)
            add_obs(db, 10, 0.5, status(submitted=0))
            acts, _ = actions(db, phase)
            self.assertEqual(kinds(acts, "rent"), [])
            self.assertEqual([b["reason"] for b in
                              kinds(acts, "blocked")],
                             ["probe_guard"])
            # a younger probe leaves renting open
            db.execute(
                "UPDATE experiment_rentals SET created_at=? WHERE "
                "order_id=10", (iso(3),))
            db.commit()
            acts, _ = actions(db, phase)
            rents = kinds(acts, "rent")
            self.assertEqual(len(rents), 1)
            self.assertEqual(rents[0]["role"], "probe")
            self.assertEqual(kinds(acts, "blocked"), [])
        finally:
            db.close()

    def test_probe_deadline_cancels_regardless(self):
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [])
            burst_id = add_burst(db, phase["phase_id"], 3)
            add_fleet(db, 10, 20)
            add_rental(db, phase["phase_id"], 10, 20,
                       CFG["probe_cancel_by_min"], role="probe",
                       burst_id=burst_id)
            add_obs(db, 10, 0.5, status(submitted=0))
            acts, _ = actions(db, phase)
            cancels = kinds(acts, "cancel")
            self.assertEqual(len(cancels), 1)
            self.assertEqual(cancels[0]["reason"], "probe_deadline")
        finally:
            db.close()

    def test_kept_and_control_get_no_rerolls(self):
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [])
            for oid, role in ((10, "kept"), (11, "control")):
                add_fleet(db, oid, 20 + oid - 10)
                add_rental(db, phase["phase_id"], oid, 20 + oid - 10,
                           30, role=role)
                add_obs(db, oid, 20, status(submitted=5))
                add_obs(db, oid, 0.5, status(submitted=5))
            acts, _ = actions(db, phase)
            self.assertEqual(kinds(acts, "restart"), [])
            self.assertEqual(kinds(acts, "cancel"), [])
        finally:
            db.close()
        # term end still ends them
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [])
            add_fleet(db, 10, 20, deadline_min=1)
            add_rental(db, phase["phase_id"], 10, 20, 30, role="kept")
            _, state = actions(db, phase)
            self.assertEqual(state["rentals"], [])
            row = db.execute(
                "SELECT end_reason FROM experiment_rentals WHERE "
                "order_id=10").fetchone()
            self.assertEqual(row["end_reason"], "term_end")
        finally:
            db.close()

    def test_trivial_judge_zero_probes(self):
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [])
            burst_id = add_burst(db, phase["phase_id"], 6)
            acts, _ = actions(db, phase)
            judges = kinds(acts, "judge")
            self.assertEqual(len(judges), 1)
            self.assertEqual(judges[0]["finders"], [])
            self.assertIsNone(judges[0]["promote"])
            self.assertIsNone(judges[0]["cancel_original"])
            self.assertEqual(judges[0]["cancel"], [])
            self.assertEqual(judges[0]["verdict"], "no_finders")
        finally:
            db.close()

    def test_probe_cancel_by_must_be_under_free_window(self):
        with self.assertRaises(ValueError):
            experiment.validate_config(
                dict(CFG, probe_cancel_by_min=CFG["free_window_min"]))
        with self.assertRaises(ValueError):
            experiment.validate_config(
                dict(CFG, probe_cancel_by_min=99))

    def test_promote_knobs_validated(self):
        for bad in (0, -0.1, 1.5):
            with self.assertRaises(ValueError):
                experiment.validate_config(
                    dict(CFG, promote_its_floor=bad))
        with self.assertRaises(ValueError):
            experiment.validate_config(dict(CFG, promote_margin=-0.01))
        ok = experiment.validate_config(
            dict(CFG, promote_its_floor=1, promote_margin=0))
        self.assertEqual(ok["promote_its_floor"], 1)
        self.assertEqual(ok["promote_margin"], 0)
        for bad in (-0.01, 1.01):
            with self.assertRaises(ValueError):
                experiment.validate_config(
                    dict(CFG, min_reliability=bad))
        self.assertEqual(experiment.validate_config(
            dict(CFG, min_reliability=0.9))["min_reliability"], 0.9)
        for bad in (0, -0.5, 1.01):
            with self.assertRaises(ValueError):
                experiment.validate_config(
                    dict(CFG, its_exponent=bad))
        self.assertEqual(
            experiment.DEFAULT_CONFIG["its_exponent"], 0.3)
        for bad in (0, -1):
            with self.assertRaises(ValueError):
                experiment.validate_config(
                    dict(CFG, its_window_min=bad))

    def test_dry_run_burst_makes_no_calls(self):
        db = make_db()
        try:
            phase = add_phase(db)
            acts = [
                {"type": "burst", "trigger": "manual",
                 "trigger_order_id": None, "n": 4},
                {"type": "judge", "burst_id": 1, "finders": [10],
                 "promote": 10, "cancel_original": None,
                 "retry": None, "verdict": "promoted",
                 "cancel": [{"order_id": 12, "server_id": 22,
                             "tier": "other",
                             "reason": "probe_no_share"}]},
                {"type": "rent", "server_id": 5, "reason": "probe",
                 "tier": "other", "role": "probe", "burst_id": 1,
                 "hours": 1},
            ]
            with mock.patch.object(
                    experiment.rent, "cancel_managed_order"
                    ) as cancel_mock, \
                    mock.patch.object(
                        experiment.rent, "quote_order") as quote_mock, \
                    mock.patch.object(
                        experiment.rent, "create_order_flow"
                    ) as create_mock, \
                    mock.patch.object(
                        experiment.orders, "fetch_wallets"
                    ) as wallet_mock:
                experiment.execute(
                    db, ":memory:", "/tmp/env", phase, CFG, acts, NOW,
                    live=False, say=lambda *_a: None)
            for m in (cancel_mock, quote_mock, create_mock,
                      wallet_mock):
                m.assert_not_called()
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM experiment_bursts"
                           ).fetchone()[0], 0)
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM experiment_events "
                           "WHERE kind='dryrun'").fetchone()[0], 3)
        finally:
            db.close()

    def test_quote_always_auto_renew_false(self):
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [listing(server_id=5)])
            with open(experiment.__file__) as fh:
                self.assertNotIn("set_renewal", fh.read())
            ctx = {"quote": {"cost_bound_usd": "0.30",
                             "hourly_cost_usd": "0.10",
                             "creation_fee_usd": "0.10"}}
            with mock.patch.object(
                    experiment.rent, "load_secrets",
                    return_value=("KEY", None)), \
                    mock.patch.object(
                        experiment.orders, "fetch_wallets",
                        return_value={"code": 0, "wallets": [
                            {"name": "USD-Blockchain", "balance": 999}
                        ]}), \
                    mock.patch.object(
                        experiment.rent, "quote_order",
                        return_value=ctx) as quote_mock, \
                    mock.patch.object(
                        experiment.rent, "create_order_flow",
                        side_effect=[(0, {"order_id": 42}),
                                     (0, {"order_id": 43})]):
                for role, hours in (("slot", None),
                                    ("probe", CFG["hours"])):
                    action = {"type": "rent", "server_id": 5,
                              "reason": "probe" if role == "probe"
                              else "fill", "tier": "other",
                              "role": role}
                    if role == "probe":
                        action["burst_id"] = 1
                        action["hours"] = hours
                    experiment.execute(
                        db, ":memory:", "/tmp/env", phase, CFG,
                        [action], NOW, live=True,
                        say=lambda *_a: None)
            for call in quote_mock.call_args_list:
                self.assertIs(call.kwargs["auto_renew"], False)
            probe_call = quote_mock.call_args_list[1]
            self.assertEqual(probe_call.kwargs["hours"], CFG["hours"])
            row = db.execute(
                "SELECT role, burst_id FROM experiment_rentals WHERE "
                "order_id=43").fetchone()
            self.assertEqual((row["role"], row["burst_id"]),
                             ("probe", 1))
        finally:
            db.close()

    def test_migration_adds_role_and_burst_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "old.sqlite3")
            old = sqlite3.connect(db_path)
            old.execute(
                "CREATE TABLE experiment_rentals("
                "order_id INTEGER PRIMARY KEY, phase_id INTEGER NOT NULL,"
                " server_id INTEGER NOT NULL, tier TEXT NOT NULL,"
                " reason TEXT NOT NULL, x_min INTEGER NOT NULL,"
                " y_min INTEGER NOT NULL, hourly_usd TEXT NOT NULL,"
                " creation_fee_usd TEXT NOT NULL,"
                " quoted_cost_usd TEXT NOT NULL, created_at TEXT NOT NULL,"
                " ended_at TEXT, end_reason TEXT)")
            old.execute(
                "INSERT INTO experiment_rentals VALUES"
                "(1, 1, 5, 'zen', 'fill', 30, 15, '0.1', '0.1', '0.7',"
                " '2026-11-15T00:00:00+00:00', NULL, NULL)")
            old.commit()
            old.close()
            db = experiment.open_db(db_path)
            try:
                cols = {r[1] for r in db.execute(
                    "PRAGMA table_info(experiment_rentals)")}
                self.assertIn("role", cols)
                self.assertIn("burst_id", cols)
                row = db.execute(
                    "SELECT role, burst_id FROM experiment_rentals"
                    ).fetchone()
                self.assertEqual((row["role"], row["burst_id"]),
                                 ("slot", None))
            finally:
                db.close()

    # -- bounded retry of healthy no-share upgrade draws -----------

    def _healthy_probe(self, db, phase, order_id=10, server_id=20,
                       hourly="0.05", probe_age=8.5, obs=None,
                       burst_trigger="upgrade"):
        """A producing incumbent plus an upgrade burst whose probe is
        healthy (fresh obs, one GPU listed and seen, measured-good
        its) but shareless -- a possible tree-luck miss. The servers
        current marketplace row gives listing_gpu_count=1."""
        self._producing_slot(db, phase)
        db.execute(
            "INSERT INTO servers(server_id, owner, country,"
            " first_seen, last_seen, latest_json)"
            " VALUES (?,?,?,?,?,?)",
            (server_id, 1, "CA", iso(500), iso(0),
             json.dumps(listing(server_id=server_id))))
        db.execute(
            "UPDATE marketplace_snapshots SET raw_json=?, server_count=1"
            " WHERE id=(SELECT MAX(id) FROM marketplace_snapshots)",
            (json.dumps({"servers": [listing(server_id=server_id)]}),))
        db.commit()
        burst_id = add_burst(db, phase["phase_id"], 6,
                             trigger=burst_trigger,
                             trigger_order_id=1, n_target=1)
        add_fleet(db, order_id, server_id)
        add_rental(db, phase["phase_id"], order_id, server_id,
                   probe_age, role="probe", burst_id=burst_id,
                   hourly=hourly)
        if obs is None:
            obs = [(8, status(submitted=0, cpu=10, gpu=80)),
                   (0.5, status(submitted=0, cpu=10, gpu=80))]
        for minutes_ago, st in obs:
            add_obs(db, order_id, minutes_ago, st)
        return burst_id

    def _tree_miss_history(self, db, phase_id, server_id=20,
                           order_id=10, created_min=66,
                           ended_min=60, judged_min=62,
                           end_reason="probe_no_share",
                           verdict="kept_original", tagged=True,
                           role="probe", trigger="upgrade",
                           started_min=90):
        """An ended probe plus its burst's judgement. tagged=True
        writes tree_retry_order_ids=[order_id]; a list writes it
        verbatim; None leaves a legacy event without the key."""
        burst_id = add_burst(db, phase_id, started_min,
                             trigger=trigger, trigger_order_id=1,
                             n_target=1, judged=True)
        add_fleet(db, order_id, server_id)
        add_rental(db, phase_id, order_id, server_id, created_min,
                   role=role, burst_id=burst_id)
        if ended_min is not None:
            db.execute(
                "UPDATE experiment_rentals SET ended_at=?,"
                " end_reason=? WHERE order_id=?",
                (iso(ended_min), end_reason, order_id))
            db.commit()
        add_event(db, phase_id, started_min, "burst_started",
                  detail={"burst_id": burst_id, "trigger": trigger,
                          "n": 1, "server_id": server_id})
        detail = {"burst_id": burst_id, "verdict": verdict}
        if tagged is True:
            detail["tree_retry_order_ids"] = [order_id]
        elif tagged is not None:
            detail["tree_retry_order_ids"] = tagged
        add_event(db, phase_id, judged_min, "burst_judged",
                  detail=detail)
        return burst_id

    def test_healthy_no_share_upgrade_probe_is_tagged(self):
        # incumbent v(90)/$0.10 = 94.9; the healthy probe measures
        # v(90)/($0.05+fee/6) = 142.3 > 94.9 * 1.25 -- only the share
        # is missing, so the judge tags it retryable
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [])
            self._healthy_probe(db, phase)
            acts, _ = actions(db, phase)
            judge = kinds(acts, "judge")[0]
            self.assertEqual(judge["verdict"], "kept_original")
            self.assertEqual(judge["tree_retry_order_ids"], [10])
            # metadata never promotes anything: the probe is still
            # cancelled inside the free window, the incumbent stays
            self.assertIsNone(judge["promote"])
            self.assertIsNone(judge["cancel_original"])
            self.assertEqual([c["order_id"] for c in judge["cancel"]],
                             [10])
            self.assertEqual(judge["cancel"][0]["reason"],
                             "probe_no_share")
            # the tag lands durably on the burst_judged event and the
            # confirmed end is recorded on the rental
            with mock.patch.object(
                    experiment.rent, "cancel_managed_order",
                    return_value=(0, {})):
                experiment.execute(
                    db, ":memory:", "/tmp/env", phase, CFG, [judge],
                    NOW, live=True, say=lambda *_a: None)
            detail = json.loads(db.execute(
                "SELECT detail_json FROM experiment_events WHERE"
                " kind='burst_judged'").fetchone()[0])
            self.assertEqual(detail["tree_retry_order_ids"], [10])
            row = db.execute(
                "SELECT ended_at, end_reason FROM experiment_rentals"
                " WHERE order_id=10").fetchone()
            self.assertEqual(row["end_reason"], "probe_no_share")
            self.assertIsNotNone(row["ended_at"])
            # freshly judged: still inside the retry cooldown
            self.assertEqual(
                experiment.upgrade_retryable_servers(
                    db, phase["phase_id"], NOW, CFG), set())
        finally:
            db.close()

    def test_tree_miss_classifier_rejects_unhealthy_probes(self):
        """Any real defect -- stale/future/missing telemetry, wrong
        GPU evidence, spec mismatch -- or an actual share means
        this was not only tree luck: no tag. Price is not a defect:
        the tag certifies hardware health, and the pick-time
        floor/return gates decide whether a re-audition spends."""
        cases = {
            # obs_age 300s > stale_obs_sec
            "stale_obs": {
                "obs": [(5, status(submitted=0, cpu=10, gpu=80))]},
            # last good sample in the future -> negative obs_age
            "future_obs": {
                "obs": [(5, status(submitted=0, cpu=10, gpu=80)),
                        (-1, status(submitted=0, cpu=10, gpu=80))]},
            # no telemetry at all
            "no_obs": {"obs": []},
            # t0 too young for the judge window
            "young_t0": {
                "obs": [(0.5, status(submitted=0, cpu=10, gpu=80))]},
            # measured gpu it/s missing
            "gpu_zero": {
                "obs": [(5, status(submitted=0, cpu=90, gpu=0)),
                        (0.5, status(submitted=0, cpu=90, gpu=0))]},
            # wrong GPU count seen vs listed
            "gpu_count": {
                "obs": [(5, status(submitted=0, cpu=10, gpu=80,
                                   gpu_count=2)),
                        (0.5, status(submitted=0, cpu=10, gpu=80,
                                     gpu_count=2))]},
            # a share arrived: this is a finder, not a tree miss
            "has_share": {
                "obs": [(5, status(submitted=1, cpu=10, gpu=80)),
                        (0.5, status(submitted=6, cpu=10, gpu=80))]},
        }
        for name, kw in cases.items():
            with self.subTest(case=name):
                db = make_db()
                try:
                    phase = add_phase(db)
                    add_snapshot(db, [])
                    self._healthy_probe(db, phase, **kw)
                    acts, _ = actions(db, phase)
                    judge = kinds(acts, "judge")[0]
                    self.assertNotIn(
                        10, judge.get("tree_retry_order_ids") or [])
                finally:
                    db.close()
        # spec mismatch: listed CPU vs the one the miner reports
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [])
            st = status(submitted=0, cpu=10, gpu=80)
            st["hardware"]["cpu_model"] = "AMD Ryzen 9 9950X"
            self._healthy_probe(
                db, phase, obs=[(5, st), (0.5, dict(st))])
            acts, _ = actions(db, phase)
            judge = kinds(acts, "judge")[0]
            self.assertNotIn(
                10, judge.get("tree_retry_order_ids") or [])
        finally:
            db.close()

    def test_tree_retry_reopens_upgrade_scan(self):
        """A server whose only phase rental was a tagged healthy
        no-share draw is tried but retryable: the upgrade scan may
        audition it again once the cooldown has passed."""
        db = make_db()
        try:
            cfg = dict(CFG, max_rentals=1)
            phase = add_phase(db, cfg=cfg)
            self._producing_slot(db, phase)
            doc = self._server_with_history(
                db, 20, "AMD Ryzen 9 9950X 16-Core Processor",
                "RTX 4090")
            doc["price"] = {"usd": {"on_demand_usd": 0.5, "spot": 0}}
            add_snapshot(db, [doc])
            self._tree_miss_history(db, phase["phase_id"])
            # the retryability lives in sqlite history, not memory:
            # a freshly rebuilt state sees it
            state = experiment.build_state(
                db, phase, NOW, cfg, say=lambda *_a: None)
            self.assertIn(20, state["tried_servers"])
            self.assertIn(20, state["upgrade_retryable"])
            self.assertNotIn(20, state["upgrade_cooldown"])
            acts, _ = actions(db, phase, cfg=cfg)
            ups = [a for a in kinds(acts, "burst")
                   if a["trigger"] == "upgrade"]
            self.assertEqual(len(ups), 1)
            self.assertEqual(ups[0]["server_id"], 20)
        finally:
            db.close()
        # a legacy judgement without the tag: still excluded as tried
        db = make_db()
        try:
            cfg = dict(CFG, max_rentals=1)
            phase = add_phase(db, cfg=cfg)
            self._producing_slot(db, phase)
            doc = self._server_with_history(
                db, 20, "AMD Ryzen 9 9950X 16-Core Processor",
                "RTX 4090")
            doc["price"] = {"usd": {"on_demand_usd": 0.5, "spot": 0}}
            add_snapshot(db, [doc])
            self._tree_miss_history(db, phase["phase_id"],
                                    tagged=None)
            state = experiment.build_state(
                db, phase, NOW, cfg, say=lambda *_a: None)
            self.assertNotIn(20, state["upgrade_retryable"])
            acts, _ = actions(db, phase, cfg=cfg)
            self.assertEqual(kinds(acts, "burst"), [])
        finally:
            db.close()

    def test_tree_retry_cooldown_boundary(self):
        """Eligibility needs both the confirmed end and the judgement
        at least upgrade_cooldown_min old."""
        db = make_db()
        try:
            phase = add_phase(db)
            bid = self._tree_miss_history(
                db, phase["phase_id"], created_min=66,
                ended_min=60, judged_min=62)
            retryable = experiment.upgrade_retryable_servers(
                db, phase["phase_id"], NOW, CFG)
            self.assertEqual(retryable, {20})
            # an end one minute younger is still cooling
            db.execute(
                "UPDATE experiment_rentals SET ended_at=? "
                "WHERE order_id=10", (iso(59),))
            db.commit()
            self.assertEqual(
                experiment.upgrade_retryable_servers(
                    db, phase["phase_id"], NOW, CFG), set())
            # and a recent judgement cools regardless of an old end
            db.execute(
                "UPDATE experiment_rentals SET ended_at=? "
                "WHERE order_id=10", (iso(60),))
            add_event(db, phase["phase_id"], 30, "burst_judged",
                      detail={"burst_id": bid,
                              "verdict": "kept_original",
                              "tree_retry_order_ids": [10]})
            db.commit()
            self.assertEqual(
                experiment.upgrade_retryable_servers(
                    db, phase["phase_id"], NOW, CFG), set())
        finally:
            db.close()

    def test_tree_retry_attempt_cap_and_phase_scope(self):
        db = make_db()
        try:
            phase = add_phase(db)
            # two confirmed misses: a third audition is still open
            for oid in (10, 12):
                self._tree_miss_history(
                    db, phase["phase_id"], order_id=oid,
                    created_min=70, ended_min=65, judged_min=63)
            self.assertEqual(
                experiment.upgrade_retryable_servers(
                    db, phase["phase_id"], NOW, CFG), {20})
            # three actual rentals ends the bounded retry
            self._tree_miss_history(
                db, phase["phase_id"], order_id=13,
                created_min=70, ended_min=65, judged_min=63)
            self.assertEqual(
                experiment.upgrade_retryable_servers(
                    db, phase["phase_id"], NOW, CFG), set())
        finally:
            db.close()
        db = make_db()
        try:
            phase = add_phase(db)
            bid = self._tree_miss_history(
                db, phase["phase_id"], order_id=10,
                created_min=70, ended_min=65, judged_min=63)
            # a duplicate judgement event doesn't add attempts: only
            # actual experiment_rentals rows count
            add_event(db, phase["phase_id"], 61, "burst_judged",
                      detail={"burst_id": bid,
                              "verdict": "kept_original",
                              "tree_retry_order_ids": [10]})
            # rows from an older phase are not part of this one
            self._tree_miss_history(db, 999, order_id=90)
            self._tree_miss_history(db, 999, order_id=91)
            self.assertEqual(
                experiment.upgrade_retryable_servers(
                    db, phase["phase_id"], NOW, CFG), {20})
            # a later event revising the verdict removes the tag
            add_event(db, phase["phase_id"], 60, "burst_judged",
                      detail={"burst_id": bid, "verdict": "upgraded"})
            self.assertEqual(
                experiment.upgrade_retryable_servers(
                    db, phase["phase_id"], NOW, CFG), set())
        finally:
            db.close()

    def test_tree_retry_requires_confirmed_tagged_probe(self):
        """Every phase burst probe must be tagged by its own burst's
        standing kept_original judgement, confirmed ended inside its
        free window, cooled for upgrade_cooldown_min."""
        for name, kw in {
            # fault/unreachable endings are verdicts, not tree luck
            "fault_end": {"end_reason": "fault:gpu_its"},
            "unreachable_end": {"end_reason": "unreachable"},
            # promoted/converted rows can never be replayed as probes
            "slot_role": {"role": "slot"},
            # still running: the end is unconfirmed
            "still_active": {"ended_min": None},
            # cancel that only landed outside the free window
            "late_end": {"created_min": 80, "ended_min": 65,
                         "judged_min": 63},
            # nonsense interval: ended before it was created
            "negative_span": {"created_min": 60, "ended_min": 65,
                              "judged_min": 63},
            # the standing judgement tagged a different order
            "other_order_tagged": {"tagged": [11]},
            # an upgraded judgement that tagged nothing re-opens
            # nothing -- the tag, not the verdict, is the evidence
            "upgraded": {"verdict": "upgraded", "tagged": []},
        }.items():
            with self.subTest(case=name):
                db = make_db()
                try:
                    phase = add_phase(db)
                    self._tree_miss_history(
                        db, phase["phase_id"], **kw)
                    self.assertEqual(
                        experiment.upgrade_retryable_servers(
                            db, phase["phase_id"], NOW, CFG), set())
                finally:
                    db.close()
        # any burst trigger counts as an audition: a tagged healthy
        # miss on a dry vacancy burst is the same tree-luck draw as
        # an upgrade burst's -- the server earns the same retry
        for name, kw in {
            "dry": {"trigger": "dry"},
            "dry_retry": {"trigger": "dry_retry"},
            "pin": {"trigger": "pin"},
        }.items():
            with self.subTest(case=name):
                db = make_db()
                try:
                    phase = add_phase(db)
                    self._tree_miss_history(
                        db, phase["phase_id"], **kw)
                    self.assertEqual(
                        experiment.upgrade_retryable_servers(
                            db, phase["phase_id"], NOW, CFG), {20})
                finally:
                    db.close()
        # malformed optional metadata skips conservatively, no crash
        for name, kw in {
            # bid unreadable: the event is skipped and the earlier
            # valid standing judgement survives
            "non_int_burst": ({"burst_id": "x"}, {20}),
            "bool_burst": ({"burst_id": True}, {20}),
            # the standing event's malformed fields fail the checks
            "str_tags": ({"tree_retry_order_ids": "10"}, set()),
            "bool_tag": ({"tree_retry_order_ids": [True]}, set()),
            "dict_tag": ({"tree_retry_order_ids": [{"a": 1}]},
                         set()),
        }.items():
            with self.subTest(case=name):
                detail, want = kw
                db = make_db()
                try:
                    phase = add_phase(db)
                    bid = self._tree_miss_history(
                        db, phase["phase_id"])
                    if "burst_id" not in detail:
                        detail["burst_id"] = bid
                        detail["verdict"] = "kept_original"
                    add_event(
                        db, phase["phase_id"], 61, "burst_judged",
                        detail=detail)
                    self.assertEqual(
                        experiment.upgrade_retryable_servers(
                            db, phase["phase_id"], NOW, CFG), want)
                finally:
                    db.close()
        # garbage detail_json and bad stored timestamps are skipped
        db = make_db()
        try:
            phase = add_phase(db)
            self._tree_miss_history(db, phase["phase_id"])
            db.execute(
                "INSERT INTO experiment_events(ts, phase_id, kind,"
                " detail_json) VALUES (?,?,?,?)",
                (iso(63), phase["phase_id"], "burst_judged",
                 "not-json"))
            db.execute(
                "UPDATE experiment_rentals SET created_at='junk'")
            db.commit()
            self.assertEqual(
                experiment.upgrade_retryable_servers(
                    db, phase["phase_id"], NOW, CFG), set())
            db.execute(
                "UPDATE experiment_rentals SET"
                " created_at='2026-11-15T10:55:00'")  # naive
            db.commit()
            self.assertEqual(
                experiment.upgrade_retryable_servers(
                    db, phase["phase_id"], NOW, CFG), set())
        finally:
            db.close()
        # a tag on a different burst does not qualify this rental
        db = make_db()
        try:
            phase = add_phase(db)
            bid = self._tree_miss_history(
                db, phase["phase_id"], order_id=10, tagged=[])
            add_event(db, phase["phase_id"], 62, "burst_judged",
                      detail={"burst_id": bid + 1,
                              "verdict": "kept_original",
                              "tree_retry_order_ids": [10]})
            self.assertEqual(
                experiment.upgrade_retryable_servers(
                    db, phase["phase_id"], NOW, CFG), set())
        finally:
            db.close()
        # a deadline-ended probe still qualifies when tagged
        db = make_db()
        try:
            phase = add_phase(db)
            self._tree_miss_history(
                db, phase["phase_id"],
                end_reason="probe_deadline")
            self.assertEqual(
                experiment.upgrade_retryable_servers(
                    db, phase["phase_id"], NOW, CFG), {20})
        finally:
            db.close()

    def test_tree_retry_blacklist_still_excludes(self):
        """Retryability only re-opens the tried_servers exclusion;
        independent gates (blacklist here) still win."""
        db = make_db()
        try:
            cfg = dict(CFG, max_rentals=1)
            phase = add_phase(db, cfg=cfg)
            self._producing_slot(db, phase)
            doc = self._server_with_history(
                db, 20, "AMD Ryzen 9 9950X 16-Core Processor",
                "RTX 4090")
            doc["price"] = {"usd": {"on_demand_usd": 0.5, "spot": 0}}
            add_snapshot(db, [doc])
            self._tree_miss_history(db, phase["phase_id"])
            blacklist.add(db, 20, "flaky_ssh", "manual")
            state = experiment.build_state(
                db, phase, NOW, cfg, say=lambda *_a: None)
            self.assertIn(20, state["upgrade_retryable"])
            self.assertIn(20, state["excluded"])
            acts, _ = actions(db, phase, cfg=cfg)
            self.assertEqual(kinds(acts, "burst"), [])
        finally:
            db.close()

    def test_upgrade_tree_max_attempts_config(self):
        self.assertEqual(
            experiment.DEFAULT_CONFIG["upgrade_tree_max_attempts"], 3)
        self.assertEqual(
            experiment.validate_config(
                {})["upgrade_tree_max_attempts"], 3)
        self.assertEqual(
            experiment.validate_config(
                dict(CFG, upgrade_tree_max_attempts=1))
            ["upgrade_tree_max_attempts"], 1)
        for bad in (True, 0, -1, 1.5, "3", None):
            with self.assertRaises(ValueError):
                experiment.validate_config(
                    dict(CFG, upgrade_tree_max_attempts=bad))


class CliTests(unittest.TestCase):
    def test_live_requires_matching_confirm_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "cq.sqlite3")
            rc = experiment.main(
                ["start-phase", "--budget-usd", "5", "--db", db_path])
            self.assertEqual(rc, 0)
            self.assertEqual(
                experiment.main(["run", "--live", "--once",
                                 "--db", db_path]), 2)
            self.assertEqual(
                experiment.main(["run", "--live", "--confirm-budget",
                                 "1", "--once", "--db", db_path]), 2)
            # matching budget proceeds (dry-run never reaches it here;
            # --live with a stubbed cycle would). Wrong: we never run
            # --live for real, so only check refusal paths.

    def test_run_loop_survives_cycle_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "cq.sqlite3")
            self.assertEqual(
                experiment.main(["start-phase", "--budget-usd", "5",
                                 "--db", db_path]), 0)
            calls = []

            def flaky(*_a, **_kw):
                calls.append(1)
                if len(calls) == 1:
                    raise ValueError("secret-laden detail")
                raise SystemExit(0)

            with mock.patch.object(experiment, "run_cycle", flaky):
                with self.assertRaises(SystemExit):
                    experiment.main(["run", "--interval", "0",
                                     "--db", db_path])
            db = experiment.open_db(db_path)
            try:
                rows = db.execute(
                    "SELECT detail_json FROM experiment_events WHERE "
                    "kind='error'").fetchall()
            finally:
                db.close()
            self.assertEqual(len(rows), 1)
            self.assertEqual(json.loads(rows[0]["detail_json"]),
                             {"error": "ValueError"})

    def test_start_phase_refuses_second_open_phase(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "cq.sqlite3")
            self.assertEqual(
                experiment.main(["start-phase", "--budget-usd", "5",
                                 "--db", db_path]), 0)
            self.assertEqual(
                experiment.main(["start-phase", "--budget-usd", "5",
                                 "--db", db_path]), 2)


class TreeTests(unittest.TestCase):
    LINE = ("2026-11-15T05:01:25.030+00:00 QUBJETSKI 2026-11-15 "
            "05:01:25.030 [INFO]  E:239 | ID: {id} | SEED: {seed}")

    def test_parse_dedupes_and_strips_ansi(self):
        text = (
            "\x1b[32m" + self.LINE.format(id="AAA", seed="S1")
            + "\x1b[0m\n"
            + self.LINE.format(id="AAA", seed="S1") + "\n"
            + "noise line\r"
            + self.LINE.format(id="BBB", seed="S2") + "\r\n"
            + self.LINE.format(id="AAA", seed="S1") + "\n")
        events = trees.parse_tree_events(text)
        self.assertEqual([e["tree_id"] for e in events],
                         ["AAA", "BBB", "AAA"])
        self.assertEqual(events[0]["epoch"], 239)
        self.assertTrue(events[0]["ts"].startswith(
            "2026-11-15T05:01:25"))

    def test_current_tree_and_segments(self):
        db = make_db()
        try:
            add_fleet(db, 1, 11)
            line = self.LINE
            fleet_store.append_log_chunk(
                db, 1, "jetski", 0,
                line.format(id="AAA", seed="S1") + "\n"
                + line.format(id="BBB", seed="S2") + "\n", 10)
            self.assertEqual(trees.current_tree(db, 1)["tree_id"], "BBB")
            segments = trees.tree_segments(db, 1)
            self.assertEqual(len(segments), 2)
            self.assertEqual(segments[0]["end_ts"],
                             segments[1]["start_ts"])
            self.assertIsNone(segments[1]["end_ts"])
        finally:
            db.close()

    def test_dead_tree_waits_for_rotation(self):
        # two prior rigs hashed on the tree and produced nothing. A
        # probe that lands it WAITS for the rotation's free redraw --
        # cancel+re-rent pays a creation fee for a draw that can hit
        # another dead offering, so the sitter rides until its tree
        # changes. tree_wait suppresses every other kill rule.
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [])
            line = (f"{iso(25)} QUBJETSKI x [INFO]  "
                    "E:240 | ID: DEADTREEX | SEED: S\n")
            for oid in (50, 51):
                add_fleet(db, oid, oid + 500)
                add_rental(db, phase["phase_id"], oid, oid + 500, 30)
                db.execute("UPDATE experiment_rentals SET ended_at=?,"
                           " end_reason='dry' WHERE order_id=?",
                           (iso(10), oid))
                fleet_store.append_log_chunk(
                    db, oid, "jetski", 0, line, len(line))
                add_obs(db, oid, 5, status(submitted=0, gpu=50))
            add_fleet(db, 1, 11)
            add_rental(db, phase["phase_id"], 1, 11, 3, role="probe",
                       burst_id=add_burst(db, phase["phase_id"], 1))
            fleet_store.append_log_chunk(
                db, 1, "jetski", 0,
                f"{iso(5)} QUBJETSKI x [INFO]  "
                "E:240 | ID: DEADTREEX | SEED: S\n", 100)
            add_obs(db, 1, 0.5, status(submitted=0, gpu=50))
            acts, _ = actions(db, phase)
            waits = kinds(acts, "tree_wait")
            self.assertEqual(len(waits), 1)
            self.assertEqual(waits[0]["tree_id"], "DEADTREEX")
            self.assertFalse(kinds(acts, "cancel"))
        finally:
            db.close()

    def test_dead_tree_stuck_cancels(self):
        # same evidence, but the rig has sat on this dead tree ~2.5
        # rotations: the rotation isn't reaching it, so it takes the
        # paid re-draw -- cancel dead_tree
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [])
            line = (f"{iso(25)} QUBJETSKI x [INFO]  "
                    "E:240 | ID: DEADTREEX | SEED: S\n")
            for oid in (50, 51):
                add_fleet(db, oid, oid + 500)
                add_rental(db, phase["phase_id"], oid, oid + 500, 60)
                db.execute("UPDATE experiment_rentals SET ended_at=?,"
                           " end_reason='dry' WHERE order_id=?",
                           (iso(10), oid))
                fleet_store.append_log_chunk(
                    db, oid, "jetski", 0, line, len(line))
                add_obs(db, oid, 5, status(submitted=0, gpu=50))
            add_fleet(db, 1, 11)
            add_rental(db, phase["phase_id"], 1, 11, 55, role="slot")
            fleet_store.append_log_chunk(
                db, 1, "jetski", 0,
                f"{iso(50)} QUBJETSKI x [INFO]  "
                "E:240 | ID: DEADTREEX | SEED: S\n", 100)
            add_obs(db, 1, 0.5, status(submitted=0, gpu=50))
            acts, _ = actions(db, phase)
            cancels = kinds(acts, "cancel")
            self.assertEqual([c["reason"] for c in cancels],
                             ["dead_tree"])
        finally:
            db.close()

    def test_tree_changed_3m_only_counts_post_restart(self):
        import io
        import contextlib
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [])
            add_fleet(db, 1, 11)
            add_rental(db, phase["phase_id"], 1, 11, 30)
            add_obs(db, 1, 20, status(submitted=1))
            add_obs(db, 1, 0.5, status(submitted=1))

            def line(minutes_ago, tree, seed="S"):
                ts = (NOW - timedelta(minutes=minutes_ago)).isoformat()
                return (f"{ts} QUBJETSKI x [INFO]  "
                        f"E:239 | ID: {tree} | SEED: {seed}\n")

            fleet_store.append_log_chunk(
                db, 1, "jetski", 0,
                line(25, "AAA") + line(20, "BBB"), 100)
            # restart during BBB, no later transition: 0
            add_event(db, phase["phase_id"], 15, "restart",
                      order_id=1, server_id=11)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                experiment._report(db, phase, CFG)
            self.assertIn("tree_changed_3m=0", buf.getvalue())
            # a restart followed by a new tree inside 3 min: 1
            # (CCC at 12.5 min ago is inside the 15-min restart's
            # window but outside the 16-min restart's)
            fleet_store.append_log_chunk(
                db, 1, "jetski", 100,
                line(12.5, "CCC", "S3"), 200)
            add_event(db, phase["phase_id"], 16, "restart",
                      order_id=1, server_id=11)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                experiment._report(db, phase, CFG)
            self.assertIn("tree_changed_3m=1", buf.getvalue())
        finally:
            db.close()

    def test_scout_reports_count_as_dead_tree_votes(self):
        # two separate report runs on the same tree where the scout
        # hashed (its>0) and shares never grew = two dry votes, so the
        # tree is dead for the fleet without a single paid landing
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [])
            # run 1 on DEADTREEX (15min span), a short stay elsewhere,
            # then run 2 on DEADTREEX (15min span)
            for t in (50, 42, 35):
                add_tree_report(db, "home", "DEADTREEX", t, its=100)
            for t in (30, 27):
                add_tree_report(db, "home", "OTHERX", t, its=100)
            for t in (20, 12, 5):
                add_tree_report(db, "home", "DEADTREEX", t, its=100)
            dead, _ = experiment._tree_stats(db, phase["phase_id"])
            self.assertIn("DEADTREEX", dead)
            self.assertNotIn("OTHERX", dead)
            # and a probe landing on it waits out the dead segment on
            # the shared evidence rather than paying a re-draw fee
            line = (f"{iso(2)} QUBJETSKI x [INFO]  "
                    "E:240 | ID: DEADTREEX | SEED: S\n")
            add_fleet(db, 1, 11)
            add_rental(db, phase["phase_id"], 1, 11, 3, role="probe",
                       burst_id=add_burst(db, phase["phase_id"], 1))
            fleet_store.append_log_chunk(db, 1, "jetski", 0, line,
                                         len(line))
            add_obs(db, 1, 0.5, status(submitted=0, gpu=50))
            acts, _ = actions(db, phase)
            self.assertEqual([w["tree_id"]
                              for w in kinds(acts, "tree_wait")],
                             ["DEADTREEX"])
        finally:
            db.close()

    def test_offering_dead_defers_probe_fill(self):
        # the scout's CURRENT tree is dead: a probe rented now draws
        # the same dead assignment, so fills wait for the rotation.
        # The deferral is observed as an offering_dead action.
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [])
            for t in (50, 42, 35):
                add_tree_report(db, "home", "DEADTREEX", t, its=100)
            for t in (30, 27):
                add_tree_report(db, "home", "OTHERX", t, its=100)
            for t in (20, 12, 3):
                add_tree_report(db, "home", "DEADTREEX", t, its=100)
            add_burst(db, phase["phase_id"], 1)
            acts, state = actions(db, phase)
            self.assertEqual(state["agent"]["tree_id"], "DEADTREEX")
            self.assertEqual(len(kinds(acts, "offering_dead")), 1)
            self.assertFalse(kinds(acts, "rent"))
        finally:
            db.close()

    def test_weak_scout_cant_convict(self):
        # a 1-thread scout (~1 it/s) never expects shares, so flat
        # shares prove nothing: no dry votes, tree stays unconvicted.
        # (If it DID land a share, that produced vote still counts.)
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [])
            for t in (50, 42, 35):
                add_tree_report(db, "home", "DEADTREEX", t, its=1)
            for t in (20, 12, 5):
                add_tree_report(db, "home", "DEADTREEX", t, its=1)
            dead, _ = experiment._tree_stats(db, phase["phase_id"])
            self.assertNotIn("DEADTREEX", dead)
        finally:
            db.close()

    def test_stale_scout_report_is_ignored(self):
        # a scout silent longer than a rotation can't speak for the
        # current offering: no defer, state.agent is None
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [])
            for t in (50, 42, 35):
                add_tree_report(db, "home", "DEADTREEX", t)
            for t in (30, 27):
                add_tree_report(db, "home", "OTHERX", t)
            for t in (25, 20):
                add_tree_report(db, "home", "DEADTREEX", t)
            add_burst(db, phase["phase_id"], 1)
            acts, state = actions(db, phase)
            self.assertIsNone(state["agent"])
            self.assertFalse(kinds(acts, "offering_dead"))
        finally:
            db.close()


class AliasDonorTests(unittest.TestCase):
    """experiment.alias_donor: which ended worker identities may be
    reused. The pool does not restore a dead worker's tree pin, so
    the only gate is that the identity be provably dead."""

    def _donor(self, db, phase_id, order_id, server_id=50,
               ended_min=30, end_reason="probe_lost",
               state="cancelled", mode="on-demand", alias=None):
        """An ended rental row pair (fleet + experiment)."""
        add_fleet(db, order_id, server_id, state=state)
        if alias is not None:
            db.execute(
                "UPDATE fleet_rentals SET alias=? WHERE order_id=?",
                (alias, order_id))
        add_rental(db, phase_id, order_id, server_id, ended_min + 20,
                   role="probe")
        db.execute(
            "UPDATE experiment_rentals SET ended_at=?, end_reason=?,"
            " mode=? WHERE order_id=?",
            (iso(ended_min), end_reason, mode, order_id))
        db.commit()

    def test_donor_basic(self):
        db = make_db()
        try:
            phase = add_phase(db)
            self._donor(db, phase["phase_id"], 1)
            d = experiment.alias_donor(db, NOW, CFG)
            self.assertIsNotNone(d)
            self.assertEqual(d["order_id"], 1)
            self.assertEqual(d["alias"], "rig-x-1")
        finally:
            db.close()

    def test_donor_needs_no_tree_history(self):
        db = make_db()
        try:
            phase = add_phase(db)
            # never logged a tree or a share -- the identity is still
            # provably dead and safe to hand off
            self._donor(db, phase["phase_id"], 1,
                        end_reason="provision_failed",
                        state="provision_failed")
            d = experiment.alias_donor(db, NOW, CFG)
            self.assertIsNotNone(d)
            self.assertEqual(d["order_id"], 1)
        finally:
            db.close()

    def test_donor_prefers_newest(self):
        db = make_db()
        try:
            phase = add_phase(db)
            self._donor(db, phase["phase_id"], 1, ended_min=60)
            self._donor(db, phase["phase_id"], 2, server_id=51,
                        ended_min=20)
            d = experiment.alias_donor(db, NOW, CFG)
            self.assertEqual(d["order_id"], 2)
        finally:
            db.close()

    def test_donor_skips_alias_still_on_open_rental(self):
        db = make_db()
        try:
            phase = add_phase(db)
            self._donor(db, phase["phase_id"], 1, alias="rig-dupe-aa")
            # an unrelated open rental happens to carry the same alias
            add_fleet(db, 2, 61)
            db.execute(
                "UPDATE fleet_rentals SET alias='rig-dupe-aa'"
                " WHERE order_id=2")
            add_rental(db, phase["phase_id"], 2, 61, 10, role="probe")
            db.commit()
            self.assertIsNone(experiment.alias_donor(db, NOW, CFG))
        finally:
            db.close()

    def test_donor_skips_alias_on_pending_attempt(self):
        db = make_db()
        try:
            phase = add_phase(db)
            self._donor(db, phase["phase_id"], 1, alias="rig-park-bb")
            db.execute(
                "INSERT INTO rental_attempts(attempt_id, server_id,"
                " alias, before_ids_json, quote_json, state,"
                " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                ("a1", 70, "rig-park-bb", "[]", "{}", "unknown",
                 iso(5), iso(5)))
            db.commit()
            self.assertIsNone(experiment.alias_donor(db, NOW, CFG))
        finally:
            db.close()

    def test_donor_skips_order_still_in_inventory(self):
        db = make_db()
        try:
            phase = add_phase(db)
            self._donor(db, phase["phase_id"], 1, end_reason="missing",
                        state="missing")
            add_inventory(db, [1])
            self.assertIsNone(experiment.alias_donor(db, NOW, CFG))
        finally:
            db.close()

    def test_donor_skips_nonterminal_state_without_inventory(self):
        db = make_db()
        try:
            phase = add_phase(db)
            # an ended rental in a non-terminal fleet state, with no
            # fresh inventory to prove the order gone, is not provably
            # dead -- its alias may still be attached somewhere
            self._donor(db, phase["phase_id"], 1,
                        end_reason="probe_lost", state="observe-only")
            self.assertIsNone(experiment.alias_donor(db, NOW, CFG))
        finally:
            db.close()

    def test_donor_skips_outbid_spot_without_fresh_inventory(self):
        db = make_db()
        try:
            phase = add_phase(db)
            self._donor(db, phase["phase_id"], 1, end_reason="outbid",
                        state="missing", mode="spot")
            # no inventory at all: the outbid bid could still resume
            self.assertIsNone(experiment.alias_donor(db, NOW, CFG))
        finally:
            db.close()

    def test_donor_outbid_ok_when_inventory_shows_it_gone(self):
        db = make_db()
        try:
            phase = add_phase(db)
            self._donor(db, phase["phase_id"], 1, end_reason="outbid",
                        state="missing", mode="spot")
            add_inventory(db, [999])
            d = experiment.alias_donor(db, NOW, CFG)
            self.assertIsNotNone(d)
            self.assertEqual(d["order_id"], 1)
        finally:
            db.close()


class ReuseProbeTests(unittest.TestCase):
    """same_tree handling for a probe that inherited a donor alias:
    with no pin survival it is an ordinary draw, so redrawing this
    server's previous tree cancels like any other probe."""

    LINE = ("{} QUBJETSKI x [INFO]  E:239 | ID: {} | SEED: S\n")

    def _log(self, db, order_id, tree, minutes_ago):
        ts = (NOW - timedelta(minutes=minutes_ago)).isoformat()
        fleet_store.append_log_chunk(
            db, order_id, "jetski", 0,
            self.LINE.format(ts, tree), 100)

    def test_reuse_probe_same_tree_cancels(self):
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [])
            # donor (server 50) died on T1; an earlier attempt on
            # server 11 drew T2. The reuse probe on 11 drew T2 too --
            # a wasted re-draw, cancelled as usual
            add_fleet(db, 1, 50, state="cancelled")
            add_rental(db, phase["phase_id"], 1, 50, 40, role="probe")
            db.execute(
                "UPDATE experiment_rentals SET ended_at=?,"
                " end_reason='probe_lost' WHERE order_id=1", (iso(30),))
            self._log(db, 1, "T1", 35)
            add_fleet(db, 2, 11, state="cancelled")
            add_rental(db, phase["phase_id"], 2, 11, 25, role="probe")
            db.execute(
                "UPDATE experiment_rentals SET ended_at=?,"
                " end_reason='probe_no_share' WHERE order_id=2",
                (iso(20),))
            self._log(db, 2, "T2", 22)
            add_fleet(db, 3, 11)
            add_rental(db, phase["phase_id"], 3, 11, 3, role="probe")
            db.execute(
                "UPDATE experiment_rentals SET reused_from=1"
                " WHERE order_id=3")
            self._log(db, 3, "T2", 2)
            add_obs(db, 3, 1, status(submitted=0))
            db.commit()
            acts, _ = actions(db, phase)
            cancels = [a for a in kinds(acts, "cancel")
                       if a.get("reason") == "same_tree"
                       and a.get("order_id") == 3]
            self.assertEqual(len(cancels), 1)
        finally:
            db.close()

    def test_reuse_probe_donor_tree_redraw_also_cancels(self):
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [])
            # donor: ended probe on server 11 that died on T1. The
            # reuse probe on the same server drew T1 again -- there is
            # no pin, so this is an accidental re-draw of the server's
            # previous tree, not a provenance success: still cancels
            add_fleet(db, 1, 11, state="cancelled")
            add_rental(db, phase["phase_id"], 1, 11, 40, role="probe")
            db.execute(
                "UPDATE experiment_rentals SET ended_at=?,"
                " end_reason='probe_lost' WHERE order_id=1", (iso(30),))
            self._log(db, 1, "T1", 35)
            add_fleet(db, 2, 11)
            add_rental(db, phase["phase_id"], 2, 11, 3, role="probe")
            db.execute(
                "UPDATE experiment_rentals SET reused_from=1"
                " WHERE order_id=2")
            self._log(db, 2, "T1", 2)
            add_obs(db, 2, 1, status(submitted=0))
            db.commit()
            acts, _ = actions(db, phase)
            cancels = [a for a in kinds(acts, "cancel")
                       if a.get("reason") == "same_tree"
                       and a.get("order_id") == 2]
            self.assertEqual(len(cancels), 1)
        finally:
            db.close()


class RestartMinerTests(unittest.TestCase):
    def test_command_and_validation(self):
        seen = {}

        def fake_run(argv, stdin=None):
            seen["argv"] = argv
            return "restarted\n"

        with mock.patch.object(rig_ssh, "_run", fake_run):
            out = rig_ssh.restart_miner(
                "rig-1.example.com", 22, "/tmp/key", 7, "jetski")
        self.assertEqual(out, "restarted")
        command = seen["argv"][-1]
        self.assertEqual(
            command,
            "pkill -TERM -f '^/clorqub/pkgs/jetski/' "
            "&& echo restarted || echo none")
        with self.assertRaises(rig_ssh.SSHError):
            rig_ssh.restart_miner("rig-1.example.com", 22, "/tmp/key",
                                  7, "evil'; rm -rf /")
        with self.assertRaises(rig_ssh.SSHError):
            rig_ssh.restart_miner("bad host!", 22, "/tmp/key", 7,
                                  "jetski")


def _next_step_r(**kw):
    r = {"role": "slot", "order_id": 1, "server_id": 11,
         "created_at": NOW - timedelta(minutes=30),
         "t0": None, "last_share_at": None, "last_restart_at": None,
         "quiet_since": None, "reroll_start": None,
         "deadline_at": NOW + timedelta(minutes=60), "x_min": 30}
    r.update(kw)
    return r


class NextStepTests(unittest.TestCase):
    def test_probe_deadline_and_judge(self):
        cfg = dict(CFG, probe_judge_min=5, probe_cancel_by_min=8)
        r = _next_step_r(role="probe",
                         created_at=NOW - timedelta(minutes=2),
                         t0=NOW - timedelta(minutes=1, seconds=54))
        self.assertEqual(experiment.next_step(r, NOW, cfg),
                         "judged in 3m")
        r = _next_step_r(role="probe",
                         created_at=NOW - timedelta(minutes=2))
        self.assertEqual(experiment.next_step(r, NOW, cfg),
                         "deadline in 6m")

    def test_kept_and_control_run_to_term_end(self):
        for role in ("kept", "control"):
            r = _next_step_r(role=role)
            self.assertEqual(
                experiment.next_step(r, NOW, CFG),
                "runs to term end in 60m")
        r = _next_step_r(role="kept", deadline_at=None)
        self.assertEqual(experiment.next_step(r, NOW, CFG),
                         "runs to term end")

    def test_booting_and_startup(self):
        self.assertEqual(
            experiment.next_step(_next_step_r(), NOW, CFG), "booting")
        r = _next_step_r(t0=NOW - timedelta(minutes=3))
        self.assertEqual(experiment.next_step(r, NOW, CFG),
                         "replace in 12m")

    def test_mining_dry_and_gives_up(self):
        r = _next_step_r(t0=NOW - timedelta(minutes=20),
                         last_share_at=NOW - timedelta(minutes=2),
                         quiet_since=NOW - timedelta(minutes=2))
        self.assertEqual(experiment.next_step(r, NOW, CFG),
                         "mining (last share 2m ago)")
        r = _next_step_r(t0=NOW - timedelta(minutes=20),
                         quiet_since=NOW - timedelta(minutes=20))
        self.assertEqual(experiment.next_step(r, NOW, CFG),
                         "replace in now")
        # produced rigs ride out their x arm before a burst
        r = _next_step_r(t0=NOW - timedelta(minutes=60),
                         last_share_at=NOW - timedelta(minutes=20),
                         quiet_since=NOW - timedelta(minutes=20))
        self.assertEqual(experiment.next_step(r, NOW, CFG),
                         "burst in 10m")
        r = _next_step_r(t0=NOW - timedelta(minutes=100),
                         last_share_at=NOW - timedelta(minutes=10),
                         quiet_since=NOW - timedelta(minutes=10),
                         last_restart_at=NOW - timedelta(minutes=1),
                         reroll_start=NOW - timedelta(minutes=1),
                         x_min=5)
        self.assertEqual(experiment.next_step(r, NOW, CFG),
                         "gives up in 4m")


class RunnerAliveTests(unittest.TestCase):
    def test_lock_states(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "db.sqlite3")
            self.assertFalse(experiment.runner_alive(db_path))
            open(db_path + ".experiment.lock", "w").close()
            self.assertFalse(experiment.runner_alive(db_path))
            fd = os.open(db_path + ".experiment.lock", os.O_RDONLY)
            try:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX)
                self.assertTrue(experiment.runner_alive(db_path))
            finally:
                os.close(fd)
            self.assertFalse(experiment.runner_alive(db_path))


class SummaryTests(unittest.TestCase):
    def test_no_tables_returns_none(self):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        self.assertIsNone(experiment.summary(db, NOW))
        db.close()

    def test_seeded_phase_snapshot(self):
        db = make_db()
        try:
            phase = add_phase(db, budget="5")
            add_fleet(db, 10, 20)
            add_rental(db, phase["phase_id"], 10, 20, 30)
            add_obs(db, 10, 20, status(submitted=3))
            add_obs(db, 10, 0.5, status(submitted=5))
            add_event(db, phase["phase_id"], 1, "restart", order_id=10,
                      server_id=20, detail={"reason": "dry",
                                            "tree_id_before": "ABC"})
            result = experiment.summary(db, NOW)
            self.assertEqual(result["phase"]["phase_id"],
                             phase["phase_id"])
            self.assertEqual(result["phase"]["budget"], "5.00")
            self.assertEqual(len(result["rentals"]), 1)
            rental = result["rentals"][0]
            for key in ("order_id", "server_id", "role", "tier",
                        "hourly_usd", "age_s", "t0", "last_share_at",
                        "shares", "tree_id", "x_min",
                        "y_min", "next"):
                self.assertIn(key, rental)
            self.assertEqual(rental["order_id"], 10)
            self.assertEqual(rental["role"], "slot")
            self.assertEqual(rental["shares"], 5)
            self.assertIn("mining (last share", rental["next"])
            self.assertEqual(result["events"][0]["kind"], "restart")
            self.assertEqual(
                result["events"][0]["detail"]["reason"], "dry")
            self.assertFalse(result["runner_alive"] is True)
            self.assertIsNone(result["burst"])
            # never writes: no ended_at was applied
            self.assertIsNone(db.execute(
                "SELECT ended_at FROM experiment_rentals"
            ).fetchone()["ended_at"])
        finally:
            db.close()

    def test_no_phase(self):
        db = make_db()
        try:
            result = experiment.summary(db, NOW)
            self.assertIsNone(result["phase"])
            self.assertEqual(result["rentals"], [])
            self.assertIn("runner_alive", result)
        finally:
            db.close()

    def test_epoch_plan_requires_dollar_calibration(self):
        """Relative-value units cannot establish expected USD profit."""
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [listing(20, price=2.4),
                              listing(21, price=4.8),
                              listing(22, price=9.6),
                              listing(23, price=1.2,
                                      reliability=0.50)])
            result = experiment.summary(db, NOW)
            self.assertIsNone(result["phase"]["epoch_plan_usd"])
            self.assertEqual(result["phase"]["epoch_plan_reason"],
                             "value_unavailable")
            # Measured speed alone still does not establish dollars.
            add_fleet(db, 10, 20)
            add_rental(db, phase["phase_id"], 10, 20, 30,
                       hourly="0.10")
            add_obs(db, 10, 0.5, status(submitted=1))
            result = experiment.summary(db, NOW)
            self.assertIsNone(result["phase"]["epoch_plan_usd"])
            self.assertIsNone(result["phase"]["epoch_plan_additional_usd"])
            # No calibration is distinct from a calibrated empty plan.
            db2 = make_db()
            try:
                add_phase(db2)
                add_snapshot(db2, [])
                result = experiment.summary(db2, NOW)
                self.assertIsNone(result["phase"]["epoch_plan_usd"])
            finally:
                db2.close()
        finally:
            db.close()

    def test_experiment_row_speed_and_keep_return(self):
        db = make_db()
        try:
            phase = add_phase(db)
            add_pool_snap(db, 0, shares=1000, counted=500,
                          usd_per_counted=0.001)
            add_snapshot(db, [])
            for oid, role, hourly in ((1, "slot", "0.10"),
                                       (2, "probe", "0.05")):
                add_fleet(db, oid, 10 + oid)
                add_rental(db, phase["phase_id"], oid, 10 + oid,
                           7, role=role, hourly=hourly)
                add_obs(db, oid, 5, status(submitted=0))
                add_obs(db, oid, 0.5, status(submitted=0))
            rows = {r["order_id"]: r for r in
                    experiment.summary(db, NOW)["rentals"]}
            self.assertEqual(rows[1]["its_total"], 90)
            self.assertEqual(rows[2]["its_total"], 90)
            # 300 sh/h * 0.5 counted/sh * $0.001/counted = $0.15/h.
            # Incumbent fee is sunk; the probe's six hours left must
            # cover a fresh $0.10 fee to earn promotion.
            self.assertAlmostEqual(rows[1]["forecast_return_x"], 1.5)
            self.assertAlmostEqual(rows[2]["forecast_return_x"], 2.25)
            with mock.patch.object(experiment, "value_model",
                                   return_value={"source": "unavailable",
                                                 "usd_hr": None}):
                rows = experiment.summary(db, NOW)["rentals"]
                self.assertTrue(all(r["forecast_return_x"] is None
                                    for r in rows))
        finally:
            db.close()

    def test_candidate_values_export_matches_pool(self):
        """candidate_values is the same scored pool _candidates returns
        on the same state, JSON-serializable, and the read leaves the
        experiment tables untouched."""
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [listing(10, price=2.4),
                              listing(11, price=4.8)])
            add_history(db, 10, fleet_order=900,
                        its_cpu=10, its_gpu=80)
            add_history(db, 11, fleet_order=901,
                        its_cpu=10, its_gpu=80)
            before = {
                t: db.execute(f"SELECT COUNT(*) c FROM {t}"
                              ).fetchone()["c"]
                for t in ("experiment_events", "experiment_rentals",
                          "experiment_bursts")}
            result = experiment.summary(db, NOW)
            for t, n in before.items():
                self.assertEqual(
                    db.execute(f"SELECT COUNT(*) c FROM {t}"
                               ).fetchone()["c"], n)
            cvs = {cv["server_id"]: cv
                   for cv in result["candidate_values"]}
            self.assertEqual(set(cvs), {10, 11})
            state = experiment.build_state(
                db, phase, NOW, CFG, say=lambda *_a: None)
            hours_to_epoch = (state["epoch_ends_at"] - NOW
                              ).total_seconds() / 3600
            hours_to_phase = max(
                0.0, (state["phase_ends_at"] - NOW
                      ).total_seconds() / 3600)
            term = max(1, min(CFG["hours"],
                              int(min(hours_to_epoch, hours_to_phase))))
            self.assertEqual(result["candidate_term_hours"], term)
            pool = {c["server_id"]: c for c in experiment._candidates(
                state, CFG, set(), [], term=term)}
            for sid, cv in cvs.items():
                self.assertEqual(cv["prior"], pool[sid]["prior"])
                self.assertEqual(cv["usd_hr"], pool[sid]["usd_hr"])
                self.assertEqual(cv["return_x"], pool[sid]["return_x"])
                self.assertEqual(cv["est_its"], pool[sid]["est_its"])
                self.assertAlmostEqual(cv["eff_hr"],
                                       pool[sid]["eff_hr"])
                self.assertAlmostEqual(cv["hourly"],
                                       float(pool[sid]["hourly"]))
                self.assertIn(
                    cv["fill_status"],
                    {"unknown value", "below floor", "below return",
                     "over budget", "fill pace", "term", "value ok"})
            # the same-est cheaper listing is the floor winner: s10
            # fills fine, s11 lands under the relative floor
            self.assertEqual(cvs[10]["fill_status"], "value ok")
            self.assertEqual(cvs[11]["fill_status"], "below floor")
            json.dumps(result["candidate_values"])
        finally:
            db.close()


class AutoBlacklistTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db()
        self.addCleanup(self.db.close)
        self.phase = add_phase(self.db)
        add_snapshot(self.db, [listing(11), listing(12)])
        add_fleet(self.db, 1, 11)
        add_rental(self.db, self.phase["phase_id"], 1, 11, 40)
        add_inventory(self.db, [1])

    def check(self):
        state = experiment.build_state(self.db, self.phase, NOW, CFG,
                                       say=lambda *_: None)
        return experiment.blacklist_checks(self.db, state, NOW, CFG)

    def test_flaky_thresholds_freshness_and_no_share_rule(self):
        for minute in range(29, -1, -1):
            add_obs(self.db, 1, minute, None if minute < 13 else status(0))
        result = self.check()
        self.assertEqual(result[0][2], "flaky_ssh")
        self.assertEqual(result[0][3]["failed"], 13)
        self.db.execute("UPDATE rig_observations SET status_json=? WHERE "
                        "captured_at=?", (json.dumps(status(0)), iso(0)))
        self.db.commit()
        self.assertEqual(self.check(), [])  # Exactly 40% is below trigger.
        self.db.execute("UPDATE rig_observations SET status_json=?", (json.dumps(status(0)),))
        self.db.commit()
        self.assertEqual(self.check(), [])  # No shares is not a server fault.
        self.db.execute("UPDATE rig_observations SET status_json=NULL")
        self.db.execute("UPDATE experiment_rentals SET created_at=?", (iso(29),))
        self.db.commit()
        self.assertEqual(self.check(), [])
        self.db.execute("UPDATE experiment_rentals SET created_at=?", (iso(40),))
        self.db.execute("DELETE FROM rig_observations WHERE captured_at<?", (iso(18),))
        self.db.commit()
        self.assertEqual(self.check(), [])  # Fewer than 20 samples.
        self.db.execute("UPDATE rig_observations SET captured_at=?", (iso(5),))
        self.db.commit()
        self.assertEqual(self.check(), [])  # Collector stopped.

    def test_dry_run_and_live_block_only_then_manual_removal(self):
        for minute in range(29, -1, -1):
            add_obs(self.db, 1, minute, None)
        with mock.patch.object(experiment.rent, "cancel_managed_order") as cancel:
            experiment.run_cycle(self.db, ":memory:", "/unused", self.phase,
                                 CFG, False, NOW, say=lambda *_: None)
            self.assertEqual(blacklist.active(self.db, NOW), {})
            cancel.assert_not_called()
        action = {"type": "blacklist", "order_id": 1, "server_id": 11,
                  "reason": "flaky_ssh", "detail": {"failed": 30, "total": 30}}
        with mock.patch.object(experiment.rent, "cancel_managed_order") as cancel:
            experiment.execute(self.db, ":memory:", "/unused", self.phase,
                               CFG, [action], NOW, True, say=lambda *_: None)
            cancel.assert_not_called()
        self.assertIn(11, blacklist.active(self.db, NOW))
        self.assertEqual(self.check(), [])
        blacklist.remove(self.db, 11, NOW)
        self.assertEqual(self.check(), [])  # Do not reblock from the same rental.

    def test_spec_mismatch_and_free_window_cancel(self):
        st = status(0)
        st["hardware"].update(cpu_model="AMD Ryzen 9 7950X", nproc=36)
        add_obs(self.db, 1, 0, st)
        self.assertEqual(self.check()[0][2], "spec_mismatch:cpu")
        st["hardware"]["cpu_model"] = "Intel Core i9-10980XE CPU @ 3.00GHz"
        st["hardware"]["nproc"] = 8
        add_obs(self.db, 1, 0, st)
        self.assertEqual(self.check()[0][2], "spec_mismatch:threads")
        self.db.execute("UPDATE experiment_rentals SET created_at=?", (iso(7),))
        self.db.commit()
        acts, _ = actions(self.db, self.phase)
        self.assertEqual(kinds(acts, "cancel")[0]["reason"], "fault:spec")
        # "18/36" lists 36 logical threads and nproc sees them all
        # (observed rigs: "16/32" -> nproc 32); nproc=18 means half
        # the threads are gone
        st["hardware"]["nproc"] = 36
        self.assertIsNone(experiment.spec_mismatch(listing(), st))
        self.assertIsNone(experiment.spec_mismatch(listing(), status()))

    def test_flaky_ssh_needs_a_live_order(self):
        """Unreachable rigs are only blacklist evidence while Clore
        still reports the order live -- a balance-spent account kills
        every rig at once and must not get servers blocked."""
        for minute in range(29, -1, -1):
            add_obs(self.db, 1, minute, None)
        self.assertEqual(self.check()[0][2], "flaky_ssh")
        # order gone from a fresh good inventory: Clore ended it
        self.db.execute("UPDATE order_inventory SET orders_json='[]'")
        self.db.commit()
        self.assertEqual(self.check(), [])
        # no usable inventory at all: unreachability is unverifiable
        self.db.execute("DELETE FROM order_inventory")
        self.db.commit()
        self.assertEqual(self.check(), [])

    def test_cancel_only_blocks_confirmed_faults(self):
        for reason, rc, expected in (("dry_reroll", 0, False),
                                     ("fault:gpu_count", 1, False),
                                     ("fault:gpu_count", 0, True),
                                     ("unreachable", 0, True)):
            with self.subTest(reason=reason, rc=rc):
                self.db.execute("DELETE FROM blacklist")
                self.db.execute("DELETE FROM experiment_events")
                self.db.commit()
                with mock.patch.object(experiment.rent, "cancel_managed_order",
                                       return_value=(rc, {})):
                    experiment._exec_cancel(
                        self.db, ":memory:", "/unused", self.phase["phase_id"],
                        {"order_id": 1, "server_id": 11, "reason": reason},
                        NOW, lambda *_: None)
                self.assertEqual(11 in blacklist.active(self.db, NOW), expected)

    def _exec_unreachable(self):
        with mock.patch.object(experiment.rent, "cancel_managed_order",
                               return_value=(0, {"order_id": 1})):
            experiment._exec_cancel(
                self.db, ":memory:", "/unused", self.phase["phase_id"],
                {"order_id": 1, "server_id": 11, "reason": "unreachable"},
                NOW, lambda *_: None)
        return self.db.execute(
            "SELECT end_reason FROM experiment_rentals WHERE order_id=1"
        ).fetchone()["end_reason"]

    def test_unreachable_cancel_when_order_gone_is_missing_not_fault(self):
        """A rig Clore ended (e.g. account balance spent) is recorded
        `missing`, keeps its re-rentability and is never blacklisted."""
        self.db.execute("UPDATE order_inventory SET orders_json='[]'")
        self.db.commit()
        self.assertEqual(self._exec_unreachable(), "missing")
        self.assertNotIn(11, blacklist.active(self.db, NOW))
        self.assertEqual(self.db.execute(
            "SELECT provision_state FROM fleet_rentals WHERE order_id=1"
        ).fetchone()["provision_state"], "missing")

    def test_unreachable_cancel_without_inventory_never_blacklists(self):
        """A stale/absent inventory can't distinguish a dead host from
        an ended order -- the cancel still lands but no block does."""
        self.db.execute("DELETE FROM order_inventory")
        self.db.commit()
        self.assertEqual(self._exec_unreachable(), "unreachable")
        self.assertNotIn(11, blacklist.active(self.db, NOW))

    def test_unreachable_cancel_pre_creation_inventory_is_ambiguous(self):
        """An inventory captured before the order existed can't prove
        it ended -- a still-provisioning order is absent from it. No
        missing mark, no blacklist."""
        self.db.execute("UPDATE order_inventory SET orders_json='[]',"
                        " captured_at=?", (iso(40),))
        self.db.commit()
        self.assertEqual(self._exec_unreachable(), "unreachable")
        self.assertNotIn(11, blacklist.active(self.db, NOW))
        self.assertEqual(self.db.execute(
            "SELECT provision_state FROM fleet_rentals WHERE order_id=1"
        ).fetchone()["provision_state"], "ready")

    def test_unreachable_cancel_already_missing_never_blacklists(self):
        """Telemetry may mark the fleet row missing between build_state
        and the cancel landing; that end is not server fault."""
        fleet_store.set_state(self.db, 1, "missing")
        self.assertEqual(self._exec_unreachable(), "missing")
        self.assertNotIn(11, blacklist.active(self.db, NOW))

    def test_provision_failure_only_and_last_moment_enforcement(self):
        ctx = {"quote": {"cost_bound_usd": "0.30"}}
        action = {"server_id": 12, "tier": "other", "reason": "fill"}
        with mock.patch.object(experiment.rent, "load_secrets", return_value=("test", {})), \
                mock.patch.object(experiment.orders, "fetch_wallets", return_value={}), \
                mock.patch.object(experiment.orders, "sanitize_wallets",
                                  return_value=[{"name": "USD-Blockchain", "balance": 100}]), \
                mock.patch.object(experiment.rent, "quote_order", return_value=ctx), \
                mock.patch.object(experiment.rent, "create_order_flow") as create:
            for info, expected in (
                    ({"order_id": None, "stage": "quote"}, False),
                    ({"order_id": 2, "stage": "register"}, False),
                    ({"order_id": 2, "stage": "provision",
                      "provision_state": "cancelled"}, True)):
                create.return_value = (1, info)
                experiment._exec_rent(self.db, ":memory:", "/unused", self.phase,
                                      CFG, action, NOW, lambda *_: None)
                self.assertEqual(12 in blacklist.active(self.db, NOW), expected)
            create.reset_mock()
            experiment._exec_rent(self.db, ":memory:", "/unused", self.phase,
                                  CFG, action, NOW, lambda *_: None)
            create.assert_not_called()

    def test_blocks_fill_probe_and_rerent(self):
        blacklist.add(self.db, 12, "test", "auto", now=NOW)
        add_obs(self.db, 1, 0, status(5))
        _, state = actions(self.db, self.phase)
        self.assertIn(12, state["excluded"])
        self.assertEqual(experiment._candidates(state, CFG, {11}, []), [])
        add_event(self.db, self.phase["phase_id"], 20, "rerent_scheduled",
                  server_id=12, detail={"server_id": 12, "tier": "other",
                                        "due_at": iso(1)})
        acts, _ = actions(self.db, self.phase)
        self.assertNotIn(12, [a["server_id"] for a in kinds(acts, "rent")])


class DecisionNoteTests(unittest.TestCase):
    """decide()'s note channel must reach experiment_events (the TUI
    log reads only events), describing the eligible pool, the pick
    and why the rest lost."""

    def _note_texts(self, db, phase_id):
        return [json.loads(r["detail_json"])["text"]
                for r in db.execute(
                    "SELECT detail_json FROM experiment_events WHERE"
                    " phase_id=? AND kind='note' ORDER BY id",
                    (phase_id,))]

    def test_fill_note_persisted_once_per_change(self):
        db = make_db()
        try:
            phase = add_phase(db)
            add_history(db, 42, fleet_order=999, its_cpu=10, its_gpu=80)
            add_snapshot(db, [listing(20), listing(21, price=4.8)])
            for _ in range(2):
                experiment.run_cycle(
                    db, ":memory:", "/unused", phase, CFG, False, NOW,
                    say=lambda *_: None)
            # the fill emits one decision note; the identical second
            # cycle must not repeat it
            notes = self._note_texts(db, phase["phase_id"])
            self.assertEqual(len(notes), 1)
            self.assertIn("fill", notes[0])
            self.assertIn("eligible", notes[0])
            self.assertIn("pick s20", notes[0])
            self.assertIn("s21", notes[0])  # runner-up shown
            self.assertIn("v/$=", notes[0])
        finally:
            db.close()

    def test_over_pace_fill_note_explains_empty_slot(self):
        db = make_db()
        try:
            phase = add_phase(db)
            pricey = listing(20, price=999.0)  # over max_price_hr cap
            add_snapshot(db, [pricey])
            experiment.run_cycle(
                db, ":memory:", "/unused", phase, CFG, False, NOW,
                say=lambda *_: None)
            notes = self._note_texts(db, phase["phase_id"])
            self.assertEqual(len(notes), 1)
            self.assertIn("none fits pace headroom", notes[0])
            self.assertIn("no eligible candidates", notes[0])
        finally:
            db.close()

    def test_first_share_logged_once(self):
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [])
            add_fleet(db, 1, 11)
            add_rental(db, phase["phase_id"], 1, 11, 8)
            add_obs(db, 1, 5, status(submitted=0))
            add_obs(db, 1, 2, status(submitted=3))
            for _ in range(2):
                experiment.build_state(db, phase, NOW, CFG,
                                       say=lambda *_: None)
            rows = db.execute(
                "SELECT detail_json FROM experiment_events WHERE"
                " phase_id=? AND kind='first_share'",
                (phase["phase_id"],)).fetchall()
            self.assertEqual(len(rows), 1)
            detail = json.loads(rows[0]["detail_json"])
            self.assertEqual(detail["role"], "slot")
            self.assertIn("its", detail)
        finally:
            db.close()


class ThrottleTests(unittest.TestCase):
    """Recurring blocked/dryrun statuses repeat at most once per
    _REPEAT_MIN per signature; different signatures never collapse."""

    def _blocked(self, db, phase_id):
        return db.execute(
            "SELECT detail_json, ts FROM experiment_events WHERE"
            " phase_id=? AND kind='blocked' ORDER BY id",
            (phase_id,)).fetchall()

    def test_blocked_throttled_within_window(self):
        db = make_db()
        try:
            phase = add_phase(db)
            pid = phase["phase_id"]
            say = lambda *_: None
            experiment._emit_blocked(db, NOW, pid, 1, 11,
                                     "telemetry_stale", say=say)
            # same signature 1 and 4 minutes later: suppressed
            experiment._emit_blocked(
                db, NOW + timedelta(minutes=1), pid, 1, 11,
                "telemetry_stale", say=say)
            experiment._emit_blocked(
                db, NOW + timedelta(minutes=4), pid, 1, 11,
                "telemetry_stale", say=say)
            self.assertEqual(len(self._blocked(db, pid)), 1)
            # past _REPEAT_MIN: emits again ('still stale' heartbeat)
            experiment._emit_blocked(
                db, NOW + timedelta(minutes=6), pid, 1, 11,
                "telemetry_stale", say=say)
            rows = self._blocked(db, pid)
            self.assertEqual(len(rows), 2)
        finally:
            db.close()

    def test_blocked_signatures_stay_distinct(self):
        db = make_db()
        try:
            phase = add_phase(db)
            pid = phase["phase_id"]
            say = lambda *_: None
            # different reason, same target
            experiment._emit_blocked(db, NOW, pid, 1, 11, "budget",
                                     say=say)
            experiment._emit_blocked(db, NOW, pid, 1, 11, "balance",
                                     say=say)
            # same reason, different order
            experiment._emit_blocked(db, NOW, pid, 2, 11, "budget",
                                     say=say)
            # same reason, no order (global block)
            experiment._emit_blocked(db, NOW, pid, None, None,
                                     "budget", say=say)
            self.assertEqual(len(self._blocked(db, pid)), 4)
        finally:
            db.close()

    def test_lull_action_emits_lull_event(self):
        # decide() is pure: an active lull surfaces as a `lull` action
        # and execute() persists the event, throttled per `until`.
        db = make_db()
        try:
            phase = add_phase(db)
            state = experiment.build_state(
                db, phase, NOW, CFG, say=lambda *_a: None)
            state["lull_until"] = NOW + timedelta(minutes=45)
            state["lull_streak"] = 2
            acts = experiment.decide(
                NOW, state, CFG, say=lambda *_a: None)
            lulls = kinds(acts, "lull")
            self.assertEqual(len(lulls), 1)
            self.assertEqual(lulls[0]["streak"], 2)
            experiment.execute(
                db, ":memory:", ":memory:", phase, CFG, acts, NOW,
                live=False, say=lambda *_a: None)
            experiment.execute(
                db, ":memory:", ":memory:", phase, CFG, acts,
                NOW + timedelta(minutes=1), live=False,
                say=lambda *_a: None)
            rows = db.execute(
                "SELECT detail_json FROM experiment_events WHERE"
                " phase_id=? AND kind='lull'",
                (phase["phase_id"],)).fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(
                json.loads(rows[0]["detail_json"])["streak"], 2)
        finally:
            db.close()

    def test_dryrun_throttled_per_action_reason(self):
        db = make_db()
        try:
            phase = add_phase(db)
            pid = phase["phase_id"]
            acts = [{"type": "cancel", "order_id": 1, "server_id": 11,
                     "reason": "dry_after_x", "rerent_at": None}]
            for _ in range(3):
                experiment.execute(
                    db, ":memory:", "/tmp/env", phase, CFG, acts, NOW,
                    live=False, say=lambda *_: None)
            rows = db.execute(
                "SELECT detail_json FROM experiment_events WHERE"
                " kind='dryrun'").fetchall()
            self.assertEqual(len(rows), 1)
            # a different reason for the same order is a new signature
            acts2 = [{"type": "cancel", "order_id": 1, "server_id": 11,
                      "reason": "probe_deadline", "rerent_at": None}]
            experiment.execute(
                db, ":memory:", "/tmp/env", phase, CFG, acts2, NOW,
                live=False, say=lambda *_: None)
            rows = db.execute(
                "SELECT detail_json FROM experiment_events WHERE"
                " kind='dryrun'").fetchall()
            self.assertEqual(len(rows), 2)
        finally:
            db.close()

    def test_cancel_event_carries_rental_context(self):
        db = make_db()
        try:
            phase = add_phase(db)
            add_fleet(db, 1, 11)
            add_rental(db, phase["phase_id"], 1, 11, 60)
            with mock.patch.object(
                    experiment.rent, "cancel_managed_order",
                    return_value=(0, {"order_id": 1})):
                experiment.execute(
                    db, ":memory:", "/tmp/env", phase, CFG,
                    [{"type": "cancel", "order_id": 1, "server_id": 11,
                      "tier": "zen", "reason": "dry_after_x",
                      "rerent_at": None, "its": 90, "shares": 12,
                      "age_min": 60}],
                    NOW, live=True, say=lambda *_: None)
            row = db.execute(
                "SELECT detail_json FROM experiment_events WHERE"
                " kind='cancel'").fetchone()
            detail = json.loads(row["detail_json"])
            self.assertEqual(detail["reason"], "dry_after_x")
            self.assertEqual(detail["its"], 90)
            self.assertEqual(detail["shares"], 12)
            self.assertEqual(detail["age_min"], 60)
        finally:
            db.close()

    def test_tick_heartbeat_once_per_window(self):
        """A healthy quiet fleet still writes a `tick` event every
        _REPEAT_MIN so the log never looks dead."""
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [])
            add_fleet(db, 1, 11)
            add_rental(db, phase["phase_id"], 1, 11, 30)
            add_obs(db, 1, 0.5, status(submitted=5))
            for now in (NOW, NOW + timedelta(minutes=1),
                        NOW + timedelta(minutes=4)):
                experiment.run_cycle(
                    db, ":memory:", "/unused", phase, CFG, False, now,
                    say=lambda *_: None)
            ticks = db.execute(
                "SELECT detail_json FROM experiment_events WHERE"
                " kind='tick'").fetchall()
            self.assertEqual(len(ticks), 1)
            detail = json.loads(ticks[0]["detail_json"])
            self.assertIn("committed", detail)
            self.assertIn("burn_hr", detail)
            experiment.run_cycle(
                db, ":memory:", "/unused", phase, CFG, False,
                NOW + timedelta(minutes=6), say=lambda *_: None)
            self.assertEqual(len(db.execute(
                "SELECT 1 FROM experiment_events WHERE kind='tick'"
                ).fetchall()), 2)
        finally:
            db.close()

    def test_ended_event_reports_final_stats(self):
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [])
            add_fleet(db, 1, 11, state="cancelled")
            add_rental(db, phase["phase_id"], 1, 11, 60)
            add_obs(db, 1, 50, status(submitted=7, cpu=30, gpu=50))
            add_obs(db, 1, 0.5, status(submitted=7, cpu=30, gpu=50))
            experiment.build_state(db, phase, NOW, CFG,
                                   say=lambda *_: None)
            row = db.execute(
                "SELECT detail_json FROM experiment_events WHERE"
                " kind='ended'").fetchone()
            detail = json.loads(row["detail_json"])
            self.assertEqual(detail["reason"], "cancelled")
            self.assertEqual(detail["its"], 80)
            self.assertEqual(detail["shares"], 7)
            self.assertEqual(detail["age_min"], 60)
        finally:
            db.close()


class ValueModelTests(unittest.TestCase):
    def _fit_fixture(self, db, n_snaps=7, spacing=70, beta=0.45,
                     k=0.5):
        """Same-epoch pool snapshots whose counted deltas were
        generated with a known beta/k; varying share proportions so
        the fit is identifiable."""
        its = {"fast": 400.0, "slow": 100.0}
        d_sols = [{"fast": 30.0, "slow": 15.0},
                  {"fast": 10.0, "slow": 40.0},
                  {"fast": 45.0, "slow": 5.0},
                  {"fast": 20.0, "slow": 25.0},
                  {"fast": 35.0, "slow": 35.0},
                  {"fast": 15.0, "slow": 50.0},
                  {"fast": 25.0, "slow": 10.0}]
        sol = {"fast": 0.0, "slow": 0.0}
        shares = counted = 0.0
        for i in range(n_snaps):
            if i:
                d = d_sols[(i - 1) % len(d_sols)]
                sol["fast"] += d["fast"]
                sol["slow"] += d["slow"]
                shares += d["fast"] + d["slow"]
                counted += k * (d["fast"] * its["fast"] ** beta
                                + d["slow"] * its["slow"] ** beta)
            add_pool_snap(
                db, (n_snaps - i) * spacing, shares=shares,
                counted=counted, usd_per_counted=0.002,
                workers={"fast": (sol["fast"], its["fast"]),
                         "slow": (sol["slow"], its["slow"])})

    def test_unavailable_without_pool_snapshot(self):
        db = make_db()
        try:
            phase = add_phase(db)
            vm = experiment.value_model(db, phase, CFG, [], now=NOW)
            self.assertEqual(vm["source"], "unavailable")
            self.assertIsNone(vm["usd_hr"])
            self.assertIsNone(vm["P"])
        finally:
            db.close()

    def test_prior_path_numbers(self):
        # S counts every rig-hour incl. dry time; the anchor is the
        # hours-weighted mean its**beta, so the curve passes through
        # the fleet's production-weighted center. 10h@200its +
        # 6h@150its -> E[its^.5] = (14.142*10 + 12.247*6)/16 = 13.43.
        db = make_db()
        try:
            phase = add_phase(db)
            add_pool_snap(db, 0, shares=1000, counted=800,
                          usd_per_counted=0.002)
            add_fleet(db, 9001, 101)
            add_rental(db, phase["phase_id"], 9001, 101, 720,
                       hourly="0.10")
            add_obs(db, 9001, 700, status(submitted=0, cpu=100,
                                          gpu=100))
            add_obs(db, 9001, 130, status(submitted=6000, cpu=100,
                                          gpu=100))
            db.execute("UPDATE experiment_rentals SET ended_at=?"
                       " WHERE order_id=?", (iso(120), 9001))
            # a dry rig: 6 rig-hours, zero shares, slower its
            add_fleet(db, 9002, 102)
            add_rental(db, phase["phase_id"], 9002, 102, 500,
                       hourly="0.10")
            add_obs(db, 9002, 480, status(submitted=0, cpu=50,
                                          gpu=100))
            add_obs(db, 9002, 150, status(submitted=0, cpu=50,
                                          gpu=100))
            db.execute("UPDATE experiment_rentals SET ended_at=?"
                       " WHERE order_id=?", (iso(140), 9002))
            db.commit()
            rows = db.execute(
                "SELECT * FROM experiment_rentals WHERE phase_id=?",
                (phase["phase_id"],)).fetchall()
            vm = experiment.value_model(db, phase, CFG, rows, now=NOW)
            self.assertEqual(vm["source"], "prior")
            self.assertAlmostEqual(vm["P"], 0.002)
            self.assertAlmostEqual(vm["w_bar"], 0.8)
            # 6000 shares over 10h + 6h of rig time
            self.assertAlmostEqual(vm["S"], 375.0)
            ref_beta = ((200 ** 0.5 * 10) + (150 ** 0.5 * 6)) / 16
            self.assertAlmostEqual(vm["its_ref"], ref_beta ** 2,
                                   places=3)
            self.assertAlmostEqual(vm["beta"], CFG["its_exponent"])
            self.assertAlmostEqual(vm["usd_hr"](180.4),
                                   375 * 0.8 * 0.002, places=3)
            self.assertAlmostEqual(vm["usd_hr"](700.0),
                                   375 * 0.8 * (700 ** 0.5 / ref_beta)
                                   * 0.002)
        finally:
            db.close()

    def test_s_falls_back_under_six_rig_hours(self):
        db = make_db()
        try:
            phase = add_phase(db)
            add_pool_snap(db, 0, usd_per_counted=0.002)
            add_fleet(db, 9001, 101)
            add_rental(db, phase["phase_id"], 9001, 101, 120,
                       hourly="0.10")
            add_obs(db, 9001, 100, status(submitted=0, cpu=50,
                                          gpu=50))
            add_obs(db, 9001, 10, status(submitted=900, cpu=50,
                                         gpu=50))
            rows = db.execute(
                "SELECT * FROM experiment_rentals WHERE phase_id=?",
                (phase["phase_id"],)).fetchall()
            vm = experiment.value_model(db, phase, CFG, rows, now=NOW)
            self.assertEqual(vm["S"], CFG["default_shares_per_hour"])
            self.assertAlmostEqual(vm["its_ref"], 100.0)
        finally:
            db.close()

    def test_its_ref_hours_weighted_ignores_dead_fast_rig(self):
        # a 0.5h dead zen rental next to a 10h healthy rig: the old
        # count-median anchor landed at (200+900)/2 = 550 and pushed
        # every forecast down ~30%; the hours-weighted anchor stays
        # near the producing rig because the dead rig barely had hours
        db = make_db()
        try:
            phase = add_phase(db)
            add_pool_snap(db, 0, shares=1000, counted=800,
                          usd_per_counted=0.002)
            add_fleet(db, 9001, 101)
            add_rental(db, phase["phase_id"], 9001, 101, 720,
                       hourly="0.10")
            add_obs(db, 9001, 700, status(submitted=0, cpu=100,
                                          gpu=100))
            add_obs(db, 9001, 130, status(submitted=6000, cpu=100,
                                          gpu=100))
            db.execute("UPDATE experiment_rentals SET ended_at=?"
                       " WHERE order_id=?", (iso(120), 9001))
            # dead fast rig: 30 min of life, no shares, 900 its
            add_fleet(db, 9002, 102)
            add_rental(db, phase["phase_id"], 9002, 102, 30,
                       hourly="0.40")
            add_obs(db, 9002, 20, status(submitted=0, cpu=800,
                                         gpu=100))
            add_obs(db, 9002, 5, status(submitted=0, cpu=800,
                                        gpu=100))
            db.execute("UPDATE experiment_rentals SET ended_at=?"
                       " WHERE order_id=?", (iso(0), 9002))
            db.commit()
            rows = db.execute(
                "SELECT * FROM experiment_rentals WHERE phase_id=?",
                (phase["phase_id"],)).fetchall()
            vm = experiment.value_model(db, phase, CFG, rows, now=NOW)
            # E[its^.5] = (200^.5*10 + 900^.5*0.5)/10.5 = 13.90
            ref_beta = ((200 ** 0.5 * 10) + (900 ** 0.5 * 0.5)) / 10.5
            self.assertAlmostEqual(vm["its_ref"], ref_beta ** 2,
                                   places=1)
            # the anchor sits near the producing rig, not the dead
            # rig's speed: usd_hr(200) is near S*wbar*P
            s = 6000 / 10.5
            self.assertAlmostEqual(vm["usd_hr"](200.0),
                                   s * 0.8 * 0.002
                                   * (200 ** 0.5 / ref_beta))
        finally:
            db.close()

    def test_fit_data_still_yields_provisional_prior(self):
        """The fit stays diagnostic: even data that recovers an
        interior beta never switches the model off the prior."""
        db = make_db()
        try:
            phase = add_phase(db)
            self._fit_fixture(db, n_snaps=7, spacing=4, beta=0.45)
            fit = pool_stats.fit_exponent(db)
            self.assertGreaterEqual(fit["n_intervals"], 6)
            self.assertAlmostEqual(fit["best_beta"], 0.45)
            vm = experiment.value_model(db, phase, CFG, [], now=NOW)
            self.assertEqual(vm["source"], "prior")
            self.assertEqual(vm["confidence"], "provisional")
            self.assertAlmostEqual(vm["beta"], CFG["its_exponent"])
            self.assertIsNone(vm["k"])
        finally:
            db.close()

    def test_prior_is_the_only_model_source(self):
        db = make_db()
        try:
            phase = add_phase(db)
            self._fit_fixture(db, n_snaps=4, spacing=4, beta=0.45)
            vm = experiment.value_model(db, phase, CFG, [], now=NOW)
            self.assertEqual(vm["source"], "prior")
            self.assertEqual(vm["confidence"], "provisional")
            self.assertAlmostEqual(vm["its_ref"], 170.0)
        finally:
            db.close()

    def test_no_fit_knobs_required(self):
        # the model and decide never read the removed fit config keys
        db = make_db()
        try:
            cfg = dict(CFG)
            cfg.pop("fit_window_min", None)
            cfg.pop("fit_min_windows", None)
            phase = add_phase(db, cfg=cfg)
            add_pool_snap(db, 0)
            state = experiment.build_state(db, phase, NOW, cfg,
                                           say=lambda *_a: None)
            self.assertEqual(state["value_model"]["source"], "prior")
            acts = experiment.decide(NOW, state, cfg,
                                     say=lambda *_a: None)
            self.assertIsNotNone(acts)
        finally:
            db.close()

    def test_stale_or_future_snapshot_unavailable(self):
        for ts_min in (31, -3):
            db = make_db()
            try:
                phase = add_phase(db)
                add_pool_snap(db, ts_min)
                vm = experiment.value_model(db, phase, CFG, [],
                                            now=NOW)
                self.assertEqual(vm["source"], "unavailable",
                                 f"ts_min={ts_min}")
            finally:
                db.close()

    def test_invalid_pool_numbers_unavailable(self):
        for shares, counted, p in ((0, 10, 0.001),
                                   (100, None, 0.001),
                                   (100, 50, None),
                                   (100, -1, 0.001),
                                   (100, 50, float("nan"))):
            db = make_db()
            try:
                phase = add_phase(db)
                add_pool_snap(db, 0, shares=shares, counted=counted,
                              usd_per_counted=p)
                vm = experiment.value_model(db, phase, CFG, [],
                                            now=NOW)
                self.assertEqual(vm["source"], "unavailable",
                                 f"shares={shares} counted={counted}"
                                 f" p={p}")
            finally:
                db.close()

    def test_candidates_carry_return_x_and_order(self):
        db = make_db()
        try:
            phase = add_phase(db)
            add_pool_snap(db, 0, usd_per_counted=0.002)
            add_history(db, 30, fleet_order=900, its_cpu=200,
                        its_gpu=800)
            add_snapshot(db, [listing(server_id=20, price=2.4),
                              listing(server_id=23, price=9.6)])
            state = experiment.build_state(db, phase, NOW, CFG,
                                           say=lambda *_a: None)
            cands = experiment._candidates(state, CFG, set(), [],
                                           term=6)
            self.assertEqual([c["server_id"] for c in cands],
                             [20, 23])
            exp_usd = 300 * 0.8 * (1000 / 170.0) ** 0.5 * 0.002
            self.assertAlmostEqual(cands[0]["usd_hr"], exp_usd)
            self.assertAlmostEqual(
                cands[0]["return_x"],
                cands[0]["usd_hr"] / cands[0]["eff_hr"])
            self.assertAlmostEqual(cands[0]["value"], exp_usd)
            self.assertGreater(cands[0]["return_x"],
                               cands[1]["return_x"])
        finally:
            db.close()

    def test_model_unavailable_keeps_relative_behaviour(self):
        db = make_db()
        try:
            phase = add_phase(db)
            add_history(db, 30, fleet_order=900, its_cpu=200,
                        its_gpu=800)
            add_snapshot(db, [listing(server_id=20, price=2.4)])
            state = experiment.build_state(db, phase, NOW, CFG,
                                           say=lambda *_a: None)
            self.assertEqual(state["value_model"]["source"],
                             "unavailable")
            cands = experiment._candidates(state, CFG, set(), [],
                                           term=6)
            self.assertIsNone(cands[0]["return_x"])
            self.assertIsNone(cands[0]["usd_hr"])
            self.assertAlmostEqual(
                cands[0]["prior"],
                1000.0 ** CFG["its_exponent"]
                / cands[0]["eff_hr"])
            # and the min_return gate is not applied
            acts, _ = actions(db, phase, cfg=dict(CFG,
                                                  min_return=99.0))
            self.assertEqual([r["server_id"]
                              for r in kinds(acts, "rent")], [20])
        finally:
            db.close()

    def test_below_return_gate_rejects_fill(self):
        db = make_db()
        try:
            # floor off: the absolute return gate alone rejects s23
            cfg = dict(CFG, min_return=5.0, min_value_ratio=0.0)
            phase = add_phase(db, cfg=cfg)
            add_pool_snap(db, 0, usd_per_counted=0.002)
            add_history(db, 30, fleet_order=900, its_cpu=200,
                        its_gpu=800)
            # s20 returns ~9.6x, s23 ~2.7x of fee-inclusive cost
            add_snapshot(db, [listing(server_id=20, price=2.4),
                              listing(server_id=23, price=9.6)])
            notes = []
            acts, _ = actions(db, phase, cfg=cfg, say=notes.append)
            self.assertEqual([r["server_id"]
                              for r in kinds(acts, "rent")], [20])
            self.assertTrue(any("below_return=1" in n
                                for n in notes))
        finally:
            db.close()
        # gate above every candidate: nothing rents, not even via the
        # idle fallback
        db = make_db()
        try:
            cfg = dict(CFG, min_return=99.0, min_value_ratio=0.0)
            phase = add_phase(db, cfg=cfg)
            add_pool_snap(db, 0, usd_per_counted=0.002)
            add_history(db, 30, fleet_order=900, its_cpu=200,
                        its_gpu=800)
            add_snapshot(db, [listing(server_id=20, price=2.4)])
            acts, _ = actions(db, phase, cfg=cfg)
            self.assertEqual(kinds(acts, "rent"), [])
        finally:
            db.close()

    def test_probe_fill_respects_min_return(self):
        db = make_db()
        try:
            cfg = dict(CFG, min_return=5.0, min_value_ratio=0.0)
            phase = add_phase(db, cfg=cfg)
            add_pool_snap(db, 0, usd_per_counted=0.002)
            add_history(db, 30, fleet_order=900, its_cpu=200,
                        its_gpu=800)
            add_snapshot(db, [listing(server_id=20, price=2.4),
                              listing(server_id=23, price=9.6)])
            # both slots filled so only the burst probe fill can rent
            for oid, sid in ((1, 11), (2, 12)):
                add_fleet(db, oid, sid)
                add_rental(db, phase["phase_id"], oid, sid, 60,
                           hourly="0.10")
                add_obs(db, oid, 0.5, status(submitted=5, cpu=10,
                                             gpu=80))
            add_burst(db, phase["phase_id"], 1, trigger="manual",
                      n_target=2)
            notes = []
            acts, _ = actions(db, phase, cfg=cfg, say=notes.append)
            probes = [r for r in kinds(acts, "rent")
                      if r.get("role") == "probe"]
            self.assertEqual([r["server_id"] for r in probes], [20])
            self.assertTrue(any("below_return=1" in n
                                for n in notes))
        finally:
            db.close()

    def test_idle_fallback_rents_shortened_term(self):
        # no pace headroom, no slot, nothing reserved: the best-return
        # listing still rents, for the longest term that fits the
        # remaining budget
        db = make_db()
        try:
            phase = add_phase(db, budget="1.00", reserve="0.00")
            add_history(db, 30, fleet_order=900, its_cpu=200,
                        its_gpu=800)
            add_snapshot(db, [listing(server_id=20, price=2.4)])
            # $0.40 spent on an ended 3h rental -> remaining $0.60:
            # bounds 0.83/0.73/0.62 for 6/5/4h don't fit, 3h at
            # $0.515 does
            add_fleet(db, 9001, 101)
            add_rental(db, phase["phase_id"], 9001, 101, 200,
                       hourly="0.10")
            db.execute("UPDATE experiment_rentals SET ended_at=?"
                       " WHERE order_id=?", (iso(20), 9001))
            db.commit()
            notes = []
            acts, _ = actions(db, phase, say=notes.append)
            rents = kinds(acts, "rent")
            self.assertEqual(len(rents), 1)
            self.assertEqual(rents[0]["server_id"], 20)
            self.assertEqual(rents[0]["hours"], 3)
            self.assertEqual(rents[0]["reason"], "fill")
            self.assertTrue(any("idle fallback s20 3h" in n
                                for n in notes))
        finally:
            db.close()

    def test_idle_fallback_does_nothing_with_active_slot(self):
        db = make_db()
        try:
            phase = add_phase(db, budget="1.00", reserve="0.00")
            add_history(db, 30, fleet_order=900, its_cpu=200,
                        its_gpu=800)
            add_snapshot(db, [listing(server_id=20, price=2.4)])
            add_fleet(db, 1, 11)
            add_rental(db, phase["phase_id"], 1, 11, 60,
                       hourly="0.50")
            add_obs(db, 1, 0.5, status(submitted=5, cpu=10, gpu=80))
            acts, _ = actions(db, phase)
            self.assertEqual(kinds(acts, "rent"), [])
        finally:
            db.close()

    def test_idle_fallback_reevaluates_return_at_shorter_term(self):
        # usd_hr $0.15/h on a $0.105/h rig returns 1.23x over 6h but
        # only 0.73x over 1h once the fresh fee lands: the budget
        # admits 1h only, the gate refuses it, nothing rents
        db = make_db()
        try:
            cfg = dict(CFG, min_value_ratio=0.0, hours=6)
            phase = add_phase(db, budget="0.36", reserve="0.00",
                              cfg=cfg)
            add_pool_snap(db, 0, shares=1000, counted=500,
                          usd_per_counted=0.001)
            add_history(db, 30, fleet_order=900, its_cpu=20,
                        its_gpu=150)
            add_snapshot(db, [listing(server_id=20, price=2.4)])
            notes = []
            acts, _ = actions(db, phase, cfg=cfg, say=notes.append)
            self.assertEqual(kinds(acts, "rent"), [])
        finally:
            db.close()
        # and the contrast: with enough budget for the 6h bound the
        # same rig's 1.23x full-term return passes and it rents
        db = make_db()
        try:
            cfg = dict(CFG, min_value_ratio=0.0, hours=6)
            phase = add_phase(db, budget="1.00", reserve="0.00",
                              cfg=cfg)
            add_pool_snap(db, 0, shares=1000, counted=500,
                          usd_per_counted=0.001)
            add_history(db, 30, fleet_order=900, its_cpu=20,
                        its_gpu=150)
            add_snapshot(db, [listing(server_id=20, price=2.4)])
            acts, _ = actions(db, phase, cfg=cfg)
            rents = kinds(acts, "rent")
            self.assertEqual(len(rents), 1)
            self.assertEqual(rents[0]["server_id"], 20)
            self.assertEqual(rents[0]["hours"], 6)
        finally:
            db.close()

    def test_probe_below_return_not_promoted_upgrade(self):
        # a producing probe that fails min_return over its remaining
        # term is a true finder (logged, dropped as probe_lost) but
        # never promoted; the healthy incumbent stays
        db = make_db()
        try:
            phase = add_phase(db)
            add_pool_snap(db, 0, shares=1000, counted=500,
                          usd_per_counted=0.001)
            add_snapshot(db, [])
            add_fleet(db, 1, 11)
            add_rental(db, phase["phase_id"], 1, 11, 60,
                       hourly="0.10")
            add_obs(db, 1, 40, status(submitted=5))
            add_obs(db, 1, 0.5, status(submitted=5))
            burst_id = add_burst(db, phase["phase_id"], 6,
                                 trigger="upgrade",
                                 trigger_order_id=1, n_target=2)
            add_fleet(db, 10, 20)
            add_rental(db, phase["phase_id"], 10, 20, 8.5,
                       role="probe", burst_id=burst_id,
                       hourly="0.10")
            # slow finder: usd_hr(40)=0.10 -> 0.86x over 6h left
            add_obs(db, 10, 5, status(submitted=1, cpu=40, gpu=0))
            add_obs(db, 10, 0.5, status(submitted=3, cpu=40, gpu=0))
            acts, _ = actions(db, phase)
            judges = kinds(acts, "judge")
            self.assertEqual(len(judges), 1)
            judge = judges[0]
            self.assertEqual(judge["verdict"], "kept_original")
            self.assertIsNone(judge["promote"])
            self.assertIsNone(judge["cancel_original"])
            self.assertEqual(judge["finders"], [10])
            self.assertEqual(
                [c["order_id"] for c in judge["cancel"]], [10])
            self.assertEqual(judge["cancel"][0]["reason"],
                             "probe_lost")
        finally:
            db.close()

    def test_probe_below_return_not_promoted_dry_retry(self):
        # no incumbent left: an uneconomic finder still can't take the
        # vacancy -- the dry_retry burst re-arms instead
        db = make_db()
        try:
            phase = add_phase(db)
            add_pool_snap(db, 0, shares=1000, counted=500,
                          usd_per_counted=0.001)
            add_snapshot(db, [])
            burst_id = add_burst(db, phase["phase_id"], 6,
                                 trigger="dry_retry",
                                 trigger_order_id=1, n_target=2)
            add_fleet(db, 10, 20)
            add_rental(db, phase["phase_id"], 10, 20, 8.5,
                       role="probe", burst_id=burst_id,
                       hourly="0.10")
            # its 50 -> usd_hr $0.081/h -> 0.70x < 1.2 gate
            add_obs(db, 10, 5, status(submitted=1, cpu=50))
            add_obs(db, 10, 0.5, status(submitted=3, cpu=50))
            acts, _ = actions(db, phase)
            judge = kinds(acts, "judge")[0]
            self.assertEqual(judge["verdict"], "retry_again")
            self.assertIsNone(judge["promote"])
            self.assertIsNotNone(judge["retry"])
        finally:
            db.close()

    def test_rerent_gated_like_fills(self):
        # a due rerent below min_reliability is skipped, not bought
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [listing(server_id=5, reliability=0.50)])
            add_event(db, phase["phase_id"], 30, "rerent_scheduled",
                      server_id=5,
                      detail={"server_id": 5,
                              "due_at": (NOW - timedelta(
                                  minutes=5)).isoformat()})
            notes = []
            acts, _ = actions(db, phase, say=notes.append)
            self.assertFalse(
                any(a.get("server_id") == 5
                    for a in kinds(acts, "rent")))
            self.assertTrue(any("rerent s5 skipped" in n
                                for n in notes))
        finally:
            db.close()
        # below min_return under the pool model
        db = make_db()
        try:
            cfg = dict(CFG, min_value_ratio=0.0)
            phase = add_phase(db, cfg=cfg)
            add_pool_snap(db, 0, shares=1000, counted=500,
                          usd_per_counted=0.001)
            add_history(db, 30, fleet_order=900, its_cpu=1,
                        its_gpu=5)
            add_snapshot(db, [listing(server_id=5)])
            add_event(db, phase["phase_id"], 30, "rerent_scheduled",
                      server_id=5,
                      detail={"server_id": 5,
                              "due_at": (NOW - timedelta(
                                  minutes=5)).isoformat()})
            notes = []
            acts, _ = actions(db, phase, cfg=cfg, say=notes.append)
            self.assertFalse(
                any(a.get("server_id") == 5
                    for a in kinds(acts, "rent")))
            self.assertTrue(any("below_return" in n for n in notes))
        finally:
            db.close()
        # over pace: the incumbent burn leaves no headroom
        db = make_db()
        try:
            phase = add_phase(db, budget="0.30", reserve="0.00")
            add_snapshot(db, [listing(server_id=5)])
            add_event(db, phase["phase_id"], 30, "rerent_scheduled",
                      server_id=5,
                      detail={"server_id": 5,
                              "due_at": (NOW - timedelta(
                                  minutes=5)).isoformat()})
            notes = []
            acts, _ = actions(db, phase, say=notes.append)
            self.assertFalse(
                any(a.get("server_id") == 5
                    for a in kinds(acts, "rent")))
            self.assertTrue(any("over_pace" in n for n in notes))
        finally:
            db.close()
        # and a valid rerent still rents
        db = make_db()
        try:
            phase = add_phase(db, budget="9.60")
            add_snapshot(db, [listing(server_id=5)])
            add_event(db, phase["phase_id"], 30, "rerent_scheduled",
                      server_id=5,
                      detail={"server_id": 5,
                              "due_at": (NOW - timedelta(
                                  minutes=5)).isoformat()})
            acts, _ = actions(db, phase)
            rents = [r for r in kinds(acts, "rent")
                     if r["reason"] == "rerent"]
            self.assertEqual([r["server_id"] for r in rents], [5])
        finally:
            db.close()

    def test_rent_under_one_hour_to_end(self):
        # 30 min left in the phase: no term block -- shares per
        # budget is the goal, so a vacancy fills with the minimum
        # 1h term
        db = make_db()
        try:
            phase = add_phase(db, ends_min=-30)
            add_history(db, 20, fleet_order=900, its_cpu=200,
                        its_gpu=800)
            add_snapshot(db, [listing(server_id=20, price=2.4)])
            acts, _ = actions(db, phase)
            rents = kinds(acts, "rent")
            self.assertEqual([r["server_id"] for r in rents], [20])
            self.assertEqual([r["hours"] for r in rents], [1])
            self.assertEqual(kinds(acts, "blocked"), [])
        finally:
            db.close()

    def test_probes_still_rent_under_one_hour_to_end(self):
        # 30 min left: an open burst still fills its probes -- the
        # minimum term quote commits budget but the probe deadline
        # bounds the burn; value gates all apply
        db = make_db()
        try:
            phase = add_phase(db, ends_min=-30)
            add_history(db, 20, fleet_order=900, its_cpu=200,
                        its_gpu=800)
            add_snapshot(db, [listing(server_id=20, price=2.4)])
            add_burst(db, phase["phase_id"], 2, trigger="manual")
            acts, _ = actions(db, phase)
            probes = [r for r in kinds(acts, "rent")
                      if r["role"] == "probe"]
            self.assertEqual([r["server_id"] for r in probes], [20])
            self.assertEqual([r["hours"] for r in probes], [1])
            self.assertEqual(kinds(acts, "blocked"), [])
        finally:
            db.close()

    def test_term_capped_by_phase_end(self):
        # 2.5h left in the phase: a fill buys a 2h term, not the
        # configured 6h
        db = make_db()
        try:
            phase = add_phase(db, ends_min=-150)
            add_history(db, 30, fleet_order=900, its_cpu=200,
                        its_gpu=800)
            add_snapshot(db, [listing(server_id=20, price=2.4)])
            acts, _ = actions(db, phase)
            rents = kinds(acts, "rent")
            self.assertEqual(len(rents), 1)
            self.assertEqual(rents[0]["hours"], 2)
        finally:
            db.close()

    def test_epoch_plan_filters_and_fee_includes_incumbents(self):
        db = make_db()
        try:
            cfg = dict(CFG, min_value_ratio=0.0, hours=6)
            phase = add_phase(db, cfg=cfg)
            add_pool_snap(db, 0, shares=1000, counted=500,
                          usd_per_counted=0.001)
            # incumbent slot at its_ref: usd_hr=0.15, ret 1.29x
            add_fleet(db, 1, 11)
            add_rental(db, phase["phase_id"], 1, 11, 60,
                       hourly="0.10")
            add_obs(db, 1, 0.5, status(submitted=5, cpu=10, gpu=80))
            add_history(db, 30, fleet_order=900, its_cpu=20,
                        its_gpu=150)
            # s20 ret 1.69x eligible; s23 ret 0.47x excluded
            add_snapshot(db, [listing(server_id=20, price=2.4),
                              listing(server_id=23, price=9.6)])
            s = experiment.summary(db, NOW)
            hours = (experiment.epoch_end(NOW) - NOW
                     ).total_seconds() / 3600
            # incumbent pays a fresh fee over the plan term, and the
            # below-return candidate is not in the plan at all
            self.assertAlmostEqual(
                float(s["phase"]["epoch_plan_usd"]),
                (0.105 + 0.10 / 6 + 0.10 + 0.10 / 6) * hours,
                places=1)
            self.assertAlmostEqual(
                float(s["phase"]["epoch_plan_gross_usd"]),
                (0.15 * (170 / 90.0) ** 0.5 + 0.15) * hours,
                places=1)
        finally:
            db.close()
        # nothing qualifying -> an explicit zero plan, not the cost
        # of the least-bad unprofitable fleet
        db = make_db()
        try:
            cfg = dict(CFG, min_value_ratio=0.0)
            phase = add_phase(db, cfg=cfg)
            add_pool_snap(db, 0, shares=1000, counted=500,
                          usd_per_counted=0.001)
            add_history(db, 30, fleet_order=900, its_cpu=20,
                        its_gpu=150)
            add_snapshot(db, [listing(server_id=23, price=9.6)])
            s = experiment.summary(db, NOW)
            self.assertEqual(s["phase"]["epoch_plan_usd"], "0.00")
            self.assertEqual(s["phase"]["epoch_plan_gross_usd"],
                             "0.00")
        finally:
            db.close()

    def test_judge_stats_use_usd_hr_when_model_available(self):
        # upgrade burst: judge stats are usd_hr/h multiples, not
        # its**0.5/$ values (which would be ~95x here)
        db = make_db()
        try:
            phase = add_phase(db)
            add_pool_snap(db, 0, usd_per_counted=0.002)
            add_snapshot(db, [])
            add_fleet(db, 1, 11)
            add_rental(db, phase["phase_id"], 1, 11, 60,
                       hourly="0.10")
            add_obs(db, 1, 40, status(submitted=5))
            add_obs(db, 1, 0.5, status(submitted=5))
            burst_id = add_burst(db, phase["phase_id"], 6,
                                 trigger="upgrade",
                                 trigger_order_id=1, n_target=2)
            add_fleet(db, 10, 20)
            add_rental(db, phase["phase_id"], 10, 20, 8.5,
                       role="probe", burst_id=burst_id,
                       hourly="0.10")
            add_obs(db, 10, 5, status(submitted=1, cpu=50))
            add_obs(db, 10, 0.5, status(submitted=3, cpu=50))
            acts, state = actions(db, phase)
            stats = kinds(acts, "judge")[0]["stats"]
            usd_hr = state["value_model"]["usd_hr"]
            self.assertAlmostEqual(
                stats["orig_value_per_usd"],
                round(usd_hr(90) / 0.10, 1))
            self.assertAlmostEqual(
                stats["probe_value_per_usd"],
                round(usd_hr(130) / (0.10 + 0.10 / 6), 1))
            self.assertLess(stats["orig_value_per_usd"], 10)
        finally:
            db.close()

    def test_summary_exposes_value_model_and_gross_plan(self):
        db = make_db()
        try:
            phase = add_phase(db)
            add_pool_snap(db, 0, usd_per_counted=0.002)
            add_history(db, 30, fleet_order=900, its_cpu=200,
                        its_gpu=800)
            add_snapshot(db, [listing(server_id=20, price=0.10)])
            s = experiment.summary(db, NOW)
            vm = s["phase"]["value_model"]
            self.assertEqual(vm["source"], "prior")
            self.assertEqual(vm["confidence"], "provisional")
            self.assertAlmostEqual(vm["S"], 300.0)
            self.assertNotIn("usd_hr", vm)
            state = experiment.build_state(db, phase, NOW, CFG,
                                           say=lambda *_a: None)
            hours = experiment.pace(state, NOW, CFG)[
                "hours_to_epoch"]
            exp_usd = 300 * 0.8 * (1000 / 170.0) ** 0.5 * 0.002
            self.assertAlmostEqual(
                float(s["phase"]["epoch_plan_gross_usd"]),
                exp_usd * hours, places=1)
            self.assertIsNotNone(s["phase"]["epoch_plan_usd"])
        finally:
            db.close()


class ProfitPlanTests(unittest.TestCase):
    def _candidate(self, sid, cost, revenue):
        return {
            "server_id": sid, "hourly": cost - 0.10 / 6,
            "eff_hr": cost, "usd_hr": revenue, "value": revenue,
            "prior": revenue / cost, "return_x": revenue / cost,
            "est_its": 100, "extrapolated": False,
        }

    def test_profit_not_return_or_gross(self):
        a = self._candidate(1, 0.10, 0.30)  # 3x; $0.20/h profit
        b = self._candidate(2, 0.20, 0.50)  # 2.5x; $0.30/h profit
        c = self._candidate(3, 0.49, 0.51)  # most gross, least profit
        plan = experiment._profit_plan([a, b, c], 10, 1.5, 1)
        self.assertEqual(plan["servers"], [2])
        self.assertAlmostEqual(plan["cost_usd"], 2)
        self.assertAlmostEqual(plan["gross_usd"], 5)
        self.assertAlmostEqual(plan["profit_usd"], 3)
        self.assertAlmostEqual(plan["additional_usd"], 0.5)
        funded = experiment._profit_plan([a, b], 10, 20, 1)
        self.assertEqual(funded["additional_usd"], 0)
        # A second slot increases net profit; a third is not assumed.
        two = experiment._profit_plan([a, b, c], 10, 20, 2)
        self.assertEqual(two["servers"], [2, 1])
        self.assertAlmostEqual(two["profit_usd"], 5)

    def test_empty_unprofitable_invalid_and_ties(self):
        profitable = self._candidate(7, 0.125, 0.375)
        tied = self._candidate(8, 0.25, 0.50)
        bad = [
            self._candidate(9, 0.20, 0.10),
            self._candidate(10, 0.20, 0.20),
            {**profitable, "usd_hr": None},
            {**profitable, "usd_hr": float("nan")},
            {**profitable, "eff_hr": float("inf")},
        ]
        for hours in (0, 10):
            plan = experiment._profit_plan(bad, hours, 1, 2)
            self.assertEqual(plan["servers"], [])
            self.assertEqual(plan["cost_usd"], 0)
            self.assertEqual(plan["profit_usd"], 0)
        plan = experiment._profit_plan([tied, profitable], 10, 5, 1)
        self.assertEqual(plan["servers"], [7])  # cheaper tie wins

    def test_summary_profit_gates_and_reserved_funds(self):
        db = make_db()
        try:
            cfg = dict(CFG, max_rentals=1)
            phase = add_phase(db, budget="5", cfg=cfg)
            add_snapshot(db, [])
            # c3 has higher profit but 2x ROI, below the 2.1x floor.
            pool = [self._candidate(1, 0.10, 0.30),
                    self._candidate(2, 0.20, 0.50),
                    self._candidate(3, 0.50, 1.00)]
            vm = {
                "source": "prior", "confidence": "provisional",
                "usd_hr": lambda its: 0.5, "P": 0.001, "w_bar": 0.5,
                "S": 300, "beta": 0.3, "k": None, "its_ref": 100,
            }
            with mock.patch.object(experiment, "_candidates",
                                   return_value=pool), \
                    mock.patch.object(experiment, "value_model",
                                      return_value=vm):
                result = experiment.summary(db, NOW)["phase"]
                # NOW has 72h until epoch end. Budget is $5, whereas
                # B's steady plan needs $14.40, earns $36, profits
                # $21.60.
                self.assertEqual(result["epoch_plan_servers"], [2])
                self.assertEqual(result["epoch_plan_usd"], "14.40")
                self.assertEqual(result["epoch_plan_gross_usd"], "36.00")
                self.assertEqual(result["epoch_plan_profit_usd"], "21.60")
                self.assertEqual(result["epoch_plan_additional_usd"], "9.40")
                self.assertTrue(result["epoch_plan_phase_limited"])
                # Committed quotes already reserve future spend:
                # $4.30 uncommitted + $0.60 active cost = $4.90.
                add_fleet(db, 10, 20)
                add_rental(db, phase["phase_id"], 10, 20, 60)
                result = experiment.summary(db, NOW)["phase"]
                self.assertEqual(result["epoch_plan_additional_usd"], "9.50")
                # A probe's temporary $0.70 quote reservation must
                # not make EP ask for $0.70 more.
                add_fleet(db, 11, 21)
                add_rental(db, phase["phase_id"], 11, 21, 1,
                           role="probe")
                result = experiment.summary(db, NOW)["phase"]
                self.assertEqual(result["epoch_plan_additional_usd"], "9.50")
                db.execute("UPDATE marketplace_snapshots SET captured_at=?",
                           (iso(20),))
                db.commit()
                result = experiment.summary(db, NOW)["phase"]
                self.assertIsNone(result["epoch_plan_usd"])
                self.assertIsNone(result["epoch_plan_profit_usd"])
                self.assertEqual(result["epoch_plan_reason"], "stale_market")
        finally:
            db.close()


class ScoreStrategyTests(unittest.TestCase):
    """cfg['score'] picks the yardstick: net = $/h after cost,
    gross = revenue $/h, its = it/s. Same fixture, same gates --
    only the score changes the winner."""

    def _flip_fixture(self, score):
        """One vacancy; s20 cheap-efficient (est 200, ~$0.07/h)
        vs s21 fast-expensive (est 500, ~$0.175/h). Default gates
        all clear both: s20 ret 1.71x, s21 ret 1.29x, floor 1.20x.
        net profit: s20 0.0677 > s21 0.0572; gross/its: s21 wins."""
        db = make_db()
        cfg = dict(CFG, max_rentals=1, score=score)
        phase = add_phase(db, budget="20", cfg=cfg)
        add_pool_snap(db, 0, shares=1000, counted=500,
                      usd_per_counted=0.001)
        cheap = add_history(db, 20, fleet_order=900, its_cpu=50,
                            its_gpu=150)
        cheap["price"] = {"usd": {"on_demand_usd": 1.60, "spot": 0}}
        fast = add_history(db, 21, fleet_order=901, its_cpu=100,
                           its_gpu=400)
        fast["price"] = {"usd": {"on_demand_usd": 4.00, "spot": 0}}
        add_snapshot(db, [cheap, fast])
        return db, phase, cfg

    def test_score_gross_flips_fill_ranking(self):
        db, phase, cfg = self._flip_fixture("net")
        try:
            acts, _ = actions(db, phase, cfg=cfg)
            self.assertEqual(
                [a["server_id"] for a in kinds(acts, "rent")], [20])
            acts, _ = actions(db, phase, cfg=dict(cfg, score="gross"))
            self.assertEqual(
                [a["server_id"] for a in kinds(acts, "rent")], [21])
        finally:
            db.close()

    def test_score_its_ranks_by_est_its(self):
        db, phase, cfg = self._flip_fixture("its")
        try:
            acts, _ = actions(db, phase, cfg=cfg)
            self.assertEqual(
                [a["server_id"] for a in kinds(acts, "rent")], [21])
        finally:
            db.close()

    def _upgrade_fixture(self, score):
        """Incumbent s11: measured its 500 on an expensive $0.30/h
        rig. Challenger s21: est its 300 at ~$0.01/h. Under `its`
        the challenger loses on the same yardstick (300 < 500x1.25);
        net sees a dominant $/h swap and fires."""
        db = make_db()
        cfg = dict(CFG, max_rentals=1, score=score)
        phase = add_phase(db, budget="20", cfg=cfg)
        add_pool_snap(db, 0, shares=1000, counted=500,
                      usd_per_counted=0.001)
        add_fleet(db, 1, 11)
        add_rental(db, phase["phase_id"], 1, 11, 60, hourly="0.30")
        add_obs(db, 1, 1, status(submitted=10, cpu=100, gpu=400))
        chal = add_history(db, 21, fleet_order=901, its_cpu=50,
                           its_gpu=250)
        chal["price"] = {"usd": {"on_demand_usd": 0.24, "spot": 0}}
        add_snapshot(db, [chal])
        return db, phase, cfg

    def test_score_its_same_metric_upgrade(self):
        db, phase, cfg = self._upgrade_fixture("its")
        try:
            notes = []
            acts, _ = actions(db, phase, cfg=cfg, say=notes.append)
            self.assertEqual(kinds(acts, "burst"), [])
            self.assertTrue(any(
                "300 it/s vs worst 500" in n for n in notes))
        finally:
            db.close()
        # net on the identical fixture takes the swap: the
        # challenger's cheaper $/h clears the negative-margin
        # incumbent -- proof the refusal above came from the
        # its yardstick, not the gates
        db, phase, cfg = self._upgrade_fixture("net")
        try:
            acts, _ = actions(db, phase, cfg=cfg)
            bursts = kinds(acts, "burst")
            self.assertEqual(len(bursts), 1)
            self.assertEqual(bursts[0]["trigger"], "upgrade")
            self.assertEqual(bursts[0]["server_id"], 21)
        finally:
            db.close()

    def test_set_strategy_expands_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "cq.sqlite3")
            self.assertEqual(
                experiment.main(["start-phase", "--budget-usd", "5",
                                 "--db", db_path]), 0)
            self.assertEqual(
                experiment.main(["set", "strategy=aggressive",
                                 "--db", db_path]), 0)
            db = experiment.open_db(db_path)
            try:
                cfg = json.loads(db.execute(
                    "SELECT config_json FROM experiment_phases"
                ).fetchone()["config_json"])
            finally:
                db.close()
            self.assertEqual(
                cfg["score"], "gross")
            self.assertEqual(cfg["min_return"], 1.05)
            self.assertEqual(cfg["min_value_ratio"], 0.5)
            self.assertEqual(cfg["upgrade_margin"], 0.15)
            self.assertEqual(cfg["promote_margin"], 0.1)
            # an individual set afterwards still wins over the bundle
            self.assertEqual(
                experiment.main(["set", "min_return=1.5",
                                 "--db", db_path]), 0)
            db = experiment.open_db(db_path)
            try:
                self.assertEqual(json.loads(db.execute(
                    "SELECT config_json FROM experiment_phases"
                ).fetchone()["config_json"])["min_return"], 1.5)
            finally:
                db.close()

    def test_set_strategy_unknown_and_score_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "cq.sqlite3")
            self.assertEqual(
                experiment.main(["start-phase", "--budget-usd", "5",
                                 "--db", db_path]), 0)
            self.assertEqual(
                experiment.main(["set", "strategy=bogus",
                                 "--db", db_path]), 2)
            # score as an ordinary key works; a bogus value is
            # rejected by validate_config before anything is stored
            self.assertEqual(
                experiment.main(["set", "score=gross",
                                 "--db", db_path]), 0)
            self.assertEqual(
                experiment.main(["set", "score=bogus",
                                 "--db", db_path]), 2)
            db = experiment.open_db(db_path)
            try:
                cfg = json.loads(db.execute(
                    "SELECT config_json FROM experiment_phases"
                ).fetchone()["config_json"])
                self.assertEqual(cfg["score"], "gross")
            finally:
                db.close()
        with self.assertRaises(ValueError):
            experiment.validate_config({"score": "bogus"})
        self.assertEqual(
            experiment.validate_config({"score": "its"})["score"],
            "its")


class GrossTiebreakTests(unittest.TestCase):
    """Under score=gross a revenue near-tie (within gross_tiebreak)
    is arbitrated on net $/h: the cheaper earner of the same gross
    takes the contest. A clear gross winner still wins on gross."""

    def _succession(self, probes):
        """A mature succession burst against a producing incumbent.

        Incumbent (order 1, s11, $0.10/h): 186 priced arrivals over
        a 56-min window -> gross ~$0.1993/h, net ~$0.0993/h; the net
        dominance bar is ~$0.1241/h. Each probe is
        (order_id, arrivals, hourly): a 7-min auditioner with one
        counted drop at t-1 -> gross = arrivals x $0.015/h over the
        4-min priced window, net = gross - hourly - fee/term."""
        db = make_db()
        cfg = dict(CFG, score="gross")
        phase = add_phase(db, cfg=cfg)
        add_pool_snap(db, 0, shares=1000, counted=500,
                      usd_per_counted=0.002)
        add_snapshot(db, [])
        add_fleet(db, 1, 11)
        add_rental(db, phase["phase_id"], 1, 11, 60, hourly="0.10")
        add_obs(db, 1, 58, status(submitted=1, cpu=10, gpu=80))
        add_obs(db, 1, 50, status(submitted=100, cpu=10, gpu=80))
        add_obs(db, 1, 1, status(submitted=187, cpu=10, gpu=80))
        burst_id = add_burst(db, phase["phase_id"], 10,
                             trigger="succession", trigger_order_id=1)
        for oid, arrivals, hourly in probes:
            add_fleet(db, oid, 10 + oid)
            add_rental(db, phase["phase_id"], oid, 10 + oid, 8.5,
                       role="probe", burst_id=burst_id, hourly=hourly)
            add_obs(db, oid, 6, status(submitted=5, cpu=50, gpu=50))
            add_obs(db, oid, 1, status(submitted=5 + arrivals,
                                       cpu=50, gpu=50))
        return db, phase, cfg

    def test_close_gross_net_winner_promotes(self):
        # probe gross 0.195 within 15% of incumbent 0.1993 but far
        # cheaper: net 0.158 > 0.124 clears the margin on net alone
        db, phase, cfg = self._succession([(10, 13, "0.02")])
        try:
            acts, _ = actions(db, phase, cfg=cfg)
            judge = kinds(acts, "judge")[0]
            self.assertEqual(judge["verdict"], "upgraded")
            self.assertEqual(judge["promote"], 10)
            self.assertEqual(judge["stats"]["tiebreak"], "net")
        finally:
            db.close()

    def test_close_gross_small_net_diff_keeps(self):
        # probe net 0.088 < 0.124 bar: arbitration fires and the
        # incumbent keeps the seat
        db, phase, cfg = self._succession([(10, 13, "0.09")])
        try:
            acts, _ = actions(db, phase, cfg=cfg)
            judge = kinds(acts, "judge")[0]
            self.assertEqual(judge["verdict"], "renewed")
            self.assertIsNone(judge["promote"])
            self.assertEqual(judge["stats"]["tiebreak"], "net")
        finally:
            db.close()

    def test_gross_not_close_margin_still_binds(self):
        # probe gross 0.24 is 20% over -- outside the near-tie -- but
        # short of the 25% margin: incumbent keeps it on gross
        db, phase, cfg = self._succession([(10, 16, "0.02")])
        try:
            acts, _ = actions(db, phase, cfg=cfg)
            judge = kinds(acts, "judge")[0]
            self.assertEqual(judge["verdict"], "renewed")
            self.assertIsNone(judge["promote"])
            self.assertIsNone(judge["stats"]["tiebreak"])
        finally:
            db.close()

    def test_gross_dominant_ignores_worse_net(self):
        # probe gross 0.27 clears the 25% margin outright: it promotes
        # even though its net (0.073) is worse than the incumbent's
        db, phase, cfg = self._succession([(10, 18, "0.18")])
        try:
            acts, _ = actions(db, phase, cfg=cfg)
            judge = kinds(acts, "judge")[0]
            self.assertEqual(judge["verdict"], "upgraded")
            self.assertEqual(judge["promote"], 10)
            self.assertIsNone(judge["stats"]["tiebreak"])
        finally:
            db.close()

    def test_close_gross_among_probes_picks_net_winner(self):
        # s20 gross 0.27 vs s21 gross 0.255 -- a near-tie between the
        # challengers: gross picks s20, the cheaper s21 wins on net
        db, phase, cfg = self._succession(
            [(10, 18, "0.18"), (11, 17, "0.02")])
        try:
            acts, _ = actions(db, phase, cfg=cfg)
            judge = kinds(acts, "judge")[0]
            self.assertEqual(judge["verdict"], "upgraded")
            self.assertEqual(judge["promote"], 11)
        finally:
            db.close()


class AttemptRecheckTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db()
        self.addCleanup(self.db.close)
        self.phase = add_phase(self.db)

    def _park(self, attempt_id="attempt-1", server_id=42,
              before_ids=(), age_min=10):
        fleet_store.begin_attempt(
            self.db, attempt_id, server_id, "rig-a", list(before_ids),
            {"hours": 2, "auto_renew": False})
        fleet_store.finish_attempt(self.db, attempt_id, "unknown")
        self.db.execute(
            "UPDATE rental_attempts SET created_at=?, updated_at=?"
            " WHERE attempt_id=?",
            (iso(age_min), iso(age_min - 1), attempt_id))
        self.db.commit()

    def _recheck(self, orders, cfg=None, minutes=0):
        with mock.patch.object(
                experiment.rent, "load_secrets",
                return_value=("key", None)), \
             mock.patch.object(
                experiment.rent, "my_orders",
                return_value=orders) as mo:
            experiment._recheck_attempts(
                self.db, "/unused", self.phase["phase_id"],
                NOW + timedelta(minutes=minutes),
                cfg or CFG, say=lambda *_: None)
            return mo

    def test_consecutive_clean_fetches_auto_clear(self):
        self._park()
        self._recheck([])
        row = self.db.execute(
            "SELECT state, recheck_count FROM rental_attempts"
        ).fetchone()
        self.assertEqual((row["state"], row["recheck_count"]),
                         ("unknown", 1))
        self._recheck([], minutes=1)  # inside the gap: not due
        row = self.db.execute(
            "SELECT recheck_count FROM rental_attempts").fetchone()
        self.assertEqual(row["recheck_count"], 1)
        self._recheck([], minutes=3)
        self._recheck([], minutes=6)
        row = self.db.execute("SELECT state FROM rental_attempts"
                              ).fetchone()
        self.assertEqual(row["state"], "no_order")
        self.assertEqual(fleet_store.pending_attempts(self.db), [])
        outcomes = [json.loads(r["detail_json"])["outcome"]
                    for r in self.db.execute(
                        "SELECT detail_json FROM experiment_events"
                        " WHERE kind='attempt_recheck' ORDER BY id")]
        self.assertEqual(outcomes[-1], "cleared_no_order")

    def test_candidate_order_stays_parked_and_resets(self):
        self._park()
        self._recheck([])  # count=1
        self._recheck([{"id": 99, "si": 42}], minutes=3)
        row = self.db.execute(
            "SELECT state, recheck_count FROM rental_attempts"
        ).fetchone()
        self.assertEqual((row["state"], row["recheck_count"]),
                         ("unknown", 0))
        self.assertEqual(len(fleet_store.pending_attempts(self.db)), 1)
        # the tally restarts after a candidate: two more clean fetches
        # are not enough to clear
        self._recheck([], minutes=6)
        self._recheck([], minutes=9)
        row = self.db.execute(
            "SELECT state, recheck_count FROM rental_attempts"
        ).fetchone()
        self.assertEqual((row["state"], row["recheck_count"]),
                         ("unknown", 2))
        self._recheck([], minutes=12)
        self.assertEqual(
            self.db.execute("SELECT state FROM rental_attempts"
                            ).fetchone()["state"], "no_order")

    def test_fresh_attempt_not_rechecked(self):
        self._park(age_min=1)  # younger than attempt_recheck_min=2
        mo = self._recheck([])
        mo.assert_not_called()
        self.assertEqual(len(fleet_store.pending_attempts(self.db)), 1)

    def test_fetch_failure_keeps_tally_and_throttles(self):
        self._park()
        self._recheck([])  # count=1
        with mock.patch.object(
                experiment.rent, "load_secrets",
                return_value=("key", None)), \
             mock.patch.object(
                experiment.rent, "my_orders",
                side_effect=RuntimeError("api down")):
            experiment._recheck_attempts(
                self.db, "/unused", self.phase["phase_id"], NOW,
                CFG, say=lambda *_: None)
        row = self.db.execute(
            "SELECT state, recheck_count, last_recheck_at"
            " FROM rental_attempts").fetchone()
        self.assertEqual((row["state"], row["recheck_count"]),
                         ("unknown", 1))  # no evidence, count kept
        self.assertIsNotNone(row["last_recheck_at"])

    def test_order_on_other_server_not_a_candidate(self):
        self._park(before_ids=[1, 2])
        for minutes in (0, 3, 6):
            self._recheck([{"id": 99, "si": 7}], minutes=minutes)
        self.assertEqual(
            self.db.execute("SELECT state FROM rental_attempts"
                            ).fetchone()["state"], "no_order")


class OperatorLeverTests(unittest.TestCase):
    """paused, slot_mode/slot_min/max_rentals, per-role spot posture
    and the spot bid premium -- the live config levers the strategy
    modal writes."""

    def test_validation(self):
        base = dict(experiment.DEFAULT_CONFIG)
        for key, bad in (("slot_mode", "bogus"),
                         ("spot_probe_mode", "bogus"),
                         ("spot_slot_mode", "bogus"),
                         ("spot_bid_premium", 0.5),
                         ("spot_bid_premium", 2.0),
                         ("paused", "yes"),
                         ("probes_enabled", "yes"),
                         ("slot_min", -1),
                         ("startup_min", -1),
                         ("dry_after_min", 0)):
            cfg = dict(base)
            cfg[key] = bad
            with self.assertRaises(ValueError, msg=key):
                experiment.validate_config(cfg)
        cfg = dict(base)
        cfg["startup_min"] = 0
        cfg["dry_after_min"] = 20
        good = experiment.validate_config(cfg)
        self.assertEqual(good["startup_min"], 0)
        self.assertEqual(good["dry_after_min"], 20)
        cfg = dict(base)
        cfg["slot_min"] = 0
        cfg["max_rentals"] = 4
        self.assertEqual(
            experiment.validate_config(cfg)["slot_min"], 0)
        # the floor clamps to the ceiling rather than failing -- a
        # config written before max_rentals was lowered still loads
        cfg["slot_min"] = 9
        cfg["max_rentals"] = 2
        self.assertEqual(
            experiment.validate_config(cfg)["slot_min"], 2)

    def test_paused_blocks_rents_keeps_cancels(self):
        cfg = dict(CFG)
        cfg["paused"] = True
        db = make_db()
        try:
            phase = add_phase(db, cfg=cfg)
            add_snapshot(db, [listing(server_id=11)])
            add_fleet(db, 1, 11)
            add_rental(db, phase["phase_id"], 1, 11, 20)
            for t in (16, 10, 2, 0.5):
                add_obs(db, 1, t, status(submitted=0))
            acts, _ = actions(db, phase, cfg=cfg)
            reasons = {a["reason"] for a in kinds(acts, "blocked")}
            self.assertIn("paused", reasons)
            self.assertEqual(kinds(acts, "rent"), [])
            self.assertEqual(kinds(acts, "burst"), [])
            self.assertEqual(
                [c["reason"] for c in kinds(acts, "cancel")], ["dry"])
        finally:
            db.close()

    def test_probes_off_suppresses_bursts_keeps_fills(self):
        """probes_enabled=off stops every audition trigger (here the
        dry incumbent's burst) while the slot lifecycle is untouched:
        a due rerent still rents and the cycle is marked
        blocked/probes_off."""
        cfg = dict(CFG)
        cfg["probes_enabled"] = False
        db = make_db()
        try:
            phase = add_phase(db, cfg=cfg)
            add_snapshot(db, [listing(server_id=5)])
            add_fleet(db, 1, 11)
            # produced once then quiet past its x arm: would normally
            # fire a dry burst against the incumbent
            add_rental(db, phase["phase_id"], 1, 11, 60, x_min=30)
            add_obs(db, 1, 40, status(submitted=5))
            add_obs(db, 1, 0.5, status(submitted=5))
            add_event(db, phase["phase_id"], 1, "rerent_scheduled",
                      server_id=5,
                      detail={"server_id": 5,
                              "due_at": (NOW - timedelta(
                                  minutes=1)).isoformat()})
            acts, _ = actions(db, phase, cfg=cfg)
            self.assertEqual(kinds(acts, "burst"), [])
            reasons = {a["reason"] for a in kinds(acts, "blocked")}
            self.assertIn("probes_off", reasons)
            rents = kinds(acts, "rent")
            self.assertEqual(len(rents), 1)
            self.assertEqual(rents[0]["server_id"], 5)
            self.assertEqual(rents[0]["reason"], "rerent")
        finally:
            db.close()

    def _two_slots(self, db, phase, shared=True, age_min=30):
        add_fleet(db, 1, 11)
        add_fleet(db, 2, 12)
        add_rental(db, phase["phase_id"], 1, 11, age_min)
        add_rental(db, phase["phase_id"], 2, 12, age_min)
        for oid in (1, 2):
            add_obs(db, oid, 5, status(submitted=0))
            add_obs(db, oid, 1, status(submitted=3 if shared else 0))

    def _slot_with_its(self, db, phase, order_id, server_id, its):
        add_fleet(db, order_id, server_id)
        add_rental(db, phase["phase_id"], order_id, server_id, 30)
        add_obs(db, order_id, 1, status(submitted=3, total=its))

    def test_slot_target_dynamic(self):
        cfg = dict(CFG)
        cfg["max_rentals"] = 4
        cfg["slot_min"] = 1
        db = make_db()
        try:
            phase = add_phase(db, cfg=cfg)
            add_snapshot(db, [listing(server_id=11),
                              listing(server_id=12)])
            _, state = actions(db, phase, cfg=cfg)
            # empty fleet: hunting floor, not zero
            self.assertEqual(state["slot_target"], 1)
            # two equal producers (score ~95): nothing stands out --
            # a normal fleet holds the floor, no growth mandate
            self._two_slots(db, phase)
            _, state = actions(db, phase, cfg=cfg)
            self.assertEqual(state["slot_target"], 1)
            # a rig scoring 190 vs peers' 95 clears the peer mean x
            # 1.25: exceptional, earns one hunting seat beyond itself
            self._slot_with_its(db, phase, 3, 13, 361)
            _, state = actions(db, phase, cfg=cfg)
            self.assertEqual(state["slot_target"], 2)
            # every further standout earns another hunting seat ...
            self._slot_with_its(db, phase, 4, 14, 361)
            _, state = actions(db, phase, cfg=cfg)
            self.assertEqual(state["slot_target"], 3)
            # ... up to the max_rentals ceiling
            self._slot_with_its(db, phase, 5, 15, 361)
            _, state = actions(db, phase, cfg=cfg)
            self.assertEqual(state["slot_target"], 4)
        finally:
            db.close()

    def test_slot_target_dynamic_uniform_strong_no_growth(self):
        # three uniformly strong rigs: none stands out from its
        # peers, so an already-good fleet earns no extra mandate --
        # growth needs a rig beating its own peers, not just being
        # good in absolute terms
        cfg = dict(CFG, slot_min=1, max_rentals=4)
        db = make_db()
        try:
            phase = add_phase(db, cfg=cfg)
            add_snapshot(db, [])
            for oid, sid in ((1, 11), (2, 12), (3, 13)):
                self._slot_with_its(db, phase, oid, sid, 361)
            _, state = actions(db, phase, cfg=cfg)
            self.assertEqual(state["slot_target"], 1)
        finally:
            db.close()

    def test_shrink_cull_cancels_weakest_excess(self):
        # over the dynamic mandate, cull evicts the weakest excess
        # rig -- one per cycle, and only one below the fleet mean so
        # a standout is never killed to meet the number
        cfg = dict(CFG, slot_min=1, max_rentals=4,
                   shrink_mode="cull")
        db = make_db()
        try:
            phase = add_phase(db, cfg=cfg)
            add_snapshot(db, [])
            # scores 50/100/150: 150 exceptional -> target 2, the 50
            # is the one excess rig and sits below the 100 mean
            self._slot_with_its(db, phase, 1, 11, 25)
            self._slot_with_its(db, phase, 2, 12, 100)
            self._slot_with_its(db, phase, 3, 13, 225)
            acts, state = actions(db, phase, cfg=cfg)
            self.assertEqual(state["slot_target"], 2)
            self.assertEqual(
                [(c["order_id"], c["reason"])
                 for c in kinds(acts, "cancel")],
                [(1, "over_target")])
        finally:
            db.close()

    def test_shrink_cull_one_victim_per_cycle(self):
        # two below-mean excess rigs: only one goes per cycle, the
        # contraction trickles instead of halving the fleet at once
        cfg = dict(CFG, slot_min=1, max_rentals=4,
                   shrink_mode="cull")
        db = make_db()
        try:
            phase = add_phase(db, cfg=cfg)
            add_snapshot(db, [])
            for oid, sid in ((1, 11), (2, 12), (3, 13)):
                self._slot_with_its(db, phase, oid, sid, 16)
            self._slot_with_its(db, phase, 4, 14, 225)
            # scores 40/40/40/150: 150 exceptional -> target 2, two
            # excess rigs both below the ~67 mean
            acts, state = actions(db, phase, cfg=cfg)
            self.assertEqual(state["slot_target"], 2)
            cancels = kinds(acts, "cancel")
            self.assertEqual(len(cancels), 1)
            self.assertEqual(cancels[0]["reason"], "over_target")
            self.assertIn(cancels[0]["order_id"], {1, 2, 3})
        finally:
            db.close()

    def test_shrink_cull_spares_unrankable(self):
        # a fresh slot can't be scored and is never picked as the
        # excess victim -- the weakest RANKABLE rig goes instead
        cfg = dict(CFG, slot_min=1, max_rentals=4,
                   shrink_mode="cull")
        db = make_db()
        try:
            phase = add_phase(db, cfg=cfg)
            add_snapshot(db, [])
            add_fleet(db, 1, 11)
            add_rental(db, phase["phase_id"], 1, 11, 3)  # < startup
            add_obs(db, 1, 1, status(submitted=1, total=25))
            self._slot_with_its(db, phase, 2, 12, 25)    # score 50
            self._slot_with_its(db, phase, 3, 13, 225)   # 150 -> exc
            acts, state = actions(db, phase, cfg=cfg)
            self.assertEqual(state["slot_target"], 2)
            self.assertEqual(
                [(c["order_id"], c["reason"])
                 for c in kinds(acts, "cancel")],
                [(2, "over_target")])
        finally:
            db.close()

    def test_shrink_no_renew_suppresses_excess_succession(self):
        # over the mandate, no_renew denies the weakest excess rig
        # its succession audition so it exits at term end while the
        # rest of the fleet renews normally
        cfg = dict(CFG, slot_min=1, max_rentals=4)
        db = make_db()
        try:
            phase = add_phase(db, cfg=cfg)
            add_snapshot(db, [])
            for oid, sid, total in ((1, 11, 25), (2, 12, 100),
                                    (3, 13, 225)):
                add_fleet(db, oid, sid, deadline_min=-15)
                add_rental(db, phase["phase_id"], oid, sid, 30)
                add_obs(db, oid, 1, status(submitted=3, total=total))
            # passive: every expiring slot arms its audition
            acts, state = actions(db, phase, cfg=cfg)
            self.assertEqual(state["slot_target"], 2)
            self.assertEqual(
                sorted(a["trigger_order_id"] for a in acts
                       if a.get("trigger") == "succession"),
                [1, 2, 3])
            # no_renew: the excess rig (order 1) is left to expire
            cfg["shrink_mode"] = "no_renew"
            acts, _ = actions(db, phase, cfg=cfg)
            self.assertEqual(
                sorted(a["trigger_order_id"] for a in acts
                       if a.get("trigger") == "succession"),
                [2, 3])
            self.assertEqual(kinds(acts, "cancel"), [])
        finally:
            db.close()

    def test_slot_target_dynamic_shrink_and_static(self):
        cfg = dict(CFG)
        cfg["max_rentals"] = 4
        cfg["slot_min"] = 1
        db = make_db()
        try:
            phase = add_phase(db, cfg=cfg)
            add_snapshot(db, [listing(server_id=11),
                              listing(server_id=12)])
            # two slots past startup but with no shares: mandate
            # shrinks to the floor and no fill is emitted
            self._two_slots(db, phase, shared=False, age_min=10)
            acts, state = actions(db, phase, cfg=cfg)
            self.assertEqual(state["slot_target"], 1)
            self.assertEqual(kinds(acts, "rent"), [])
            cfg["slot_mode"] = "static"
            _, state = actions(db, phase, cfg=cfg)
            self.assertEqual(state["slot_target"], 4)
        finally:
            db.close()

    def _spot_quote_row(self, db, sid, floor=1.0, offers=()):
        import spot
        db.executescript(spot.SCHEMA)
        db.execute(
            "INSERT OR IGNORE INTO servers(server_id, owner, country,"
            " first_seen, last_seen, latest_json)"
            " VALUES (?,?,?,?,?,?)",
            (sid, 1, "CA", iso(60), iso(1),
             json.dumps(listing(server_id=sid))))
        payload = {"code": 0, "exists": True, "market": {
            "server": {"online": True, "visibility": "public",
                       "min_pricing": {"USD-Blockchain": floor}},
            "offers": [
                {"currency": "USD-Blockchain", "bid": b, "my": False}
                for b in offers]}}
        db.execute(
            "INSERT INTO spot_quotes(server_id, captured_at,"
            " raw_json, error_kind) VALUES (?,?,?,NULL)",
            (sid, iso(1), json.dumps(payload)))
        db.commit()

    def test_candidates_spot_mode_off_stays_on_demand(self):
        cfg = dict(CFG)
        cfg["spot_enabled"] = True
        db = make_db()
        try:
            phase = add_phase(db, cfg=cfg)
            add_snapshot(db, [listing(server_id=11)])
            self._spot_quote_row(db, 11)
            _, state = actions(db, phase, cfg=cfg)
            self.assertIn(11, state["spot_quotes"])
            auto = experiment._candidates(
                state, cfg, set(), [], term=1)
            self.assertEqual(auto[0]["mode"], "spot")
            off = experiment._candidates(
                state, cfg, set(), [], term=1, spot_mode="off")
            self.assertEqual(off[0]["mode"], "on-demand")
        finally:
            db.close()

    def test_bid_for_premium(self):
        import spot
        db = make_db()
        try:
            self._spot_quote_row(db, 11, floor=0.8, offers=(1.0,))
            rec = dict(db.execute(
                "SELECT * FROM spot_quotes WHERE server_id=11"
            ).fetchone())
            self.assertAlmostEqual(
                spot.bid_for(rec, NOW, premium=1.05)["bid_usd_day"],
                1.05)
            self.assertAlmostEqual(
                spot.bid_for(rec, NOW)["bid_usd_day"], 1.01)
        finally:
            db.close()


class PoolShareEstimateTests(unittest.TestCase):
    EXTRA = {"pool_total_sol": 280000.0, "pool_total_its": 56000.0,
             "pool_miners": 2500, "epoch_progress": "50.0%"}

    def _rentals(self):
        return [
            {"its_total": 200.0, "hourly_usd": "0.10",
             "share_arrivals": [("t", 5)]},   # 2000 its per $/hr
            {"its_total": 400.0, "hourly_usd": "0.10",
             "share_arrivals": [("t", 3)]},   # 4000
            {"its_total": 100.0, "hourly_usd": "0.05",
             "share_arrivals": []},           # unproven, excluded
        ]

    def test_measured_path(self):
        db = make_db()
        try:
            add_pool_snap(db, 10, workers={"a": (200.0, 100.0)},
                          extra=self.EXTRA)
            est = experiment.pool_share_estimate(
                db, self._rentals(),
                dict(CFG, pool_share_enabled=True,
                     pool_share_target_pct=1.0), now=NOW)
            # measured sol/its = 200/100; pool ratio (5.0) unused
            self.assertAlmostEqual(est["sol_per_its"], 2.0)
            self.assertAlmostEqual(est["needed_sol"], 2800.0)
            self.assertAlmostEqual(est["needed_its"], 1400.0)
            # median(2000, 4000) = 3000 -> 1400/3000 $/hr
            self.assertAlmostEqual(est["eff_its_per_usd"], 3000.0)
            self.assertEqual(est["eff_n"], 2)
            self.assertAlmostEqual(est["est_cost_hr"],
                                   1400.0 / 3000.0)
            self.assertAlmostEqual(est["est_cost_day"],
                                   1400.0 / 3000.0 * 24)
            self.assertAlmostEqual(est["epoch_h_left"], 84.0)
            self.assertAlmostEqual(est["est_cost_epoch"],
                                   1400.0 / 3000.0 * 84)
            self.assertAlmostEqual(est["share_now_pct"],
                                   100 * 200.0 / 280000.0)
            self.assertAlmostEqual(est["our_sol"], 200.0)
            self.assertEqual(est["pool_miners"], 2500)
            self.assertAlmostEqual(est["snapshot_age_s"], 600.0)
        finally:
            db.close()

    def test_off_target_zeroes_needs(self):
        db = make_db()
        try:
            add_pool_snap(db, 1, workers={"a": (200.0, 100.0)},
                          extra=self.EXTRA)
            est = experiment.pool_share_estimate(
                db, self._rentals(),
                dict(CFG, pool_share_enabled=True,
                     pool_share_target_pct=0), now=NOW)
            self.assertEqual(est["needed_sol"], 0.0)
            self.assertEqual(est["needed_its"], 0.0)
            self.assertEqual(est["est_cost_hr"], 0.0)
            self.assertEqual(est["target_pct"], 0.0)
            self.assertIsNotNone(est["share_now_pct"])
        finally:
            db.close()

    def test_missing_snapshot_returns_none(self):
        db = make_db()
        try:
            self.assertIsNone(experiment.pool_share_estimate(
                db, [], CFG, now=NOW))
        finally:
            db.close()

    def test_pool_ratio_fallback_and_unproven_eff(self):
        db = make_db()
        try:
            add_pool_snap(db, 1, workers=None, extra=self.EXTRA)
            est = experiment.pool_share_estimate(
                db, [], dict(CFG, pool_share_enabled=True,
                     pool_share_target_pct=2.0), now=NOW)
            # no workers -> pool-wide ratio 280000/56000 = 5.0
            self.assertAlmostEqual(est["sol_per_its"], 5.0)
            self.assertAlmostEqual(est["needed_sol"], 5600.0)
            self.assertAlmostEqual(est["needed_its"], 1120.0)
            # no proven stints -> costs stay unknown, needs still show
            self.assertIsNone(est["eff_its_per_usd"])
            self.assertIsNone(est["est_cost_hr"])
            self.assertIsNone(est["est_cost_day"])
            self.assertIsNone(est["est_cost_epoch"])
            self.assertEqual(est["eff_n"], 0)
            self.assertEqual(est["our_sol"], 0)
        finally:
            db.close()

    def test_no_epoch_progress_means_no_epoch_cost(self):
        db = make_db()
        try:
            add_pool_snap(
                db, 1, workers={"a": (200.0, 100.0)},
                extra=dict(self.EXTRA, epoch_progress=None))
            est = experiment.pool_share_estimate(
                db, self._rentals(),
                dict(CFG, pool_share_enabled=True,
                     pool_share_target_pct=1.0), now=NOW)
            self.assertIsNone(est["epoch_h_left"])
            self.assertIsNone(est["est_cost_epoch"])
            self.assertIsNotNone(est["est_cost_hr"])
        finally:
            db.close()

    def test_disabled_still_estimates_but_never_warns(self):
        """Off = what-if view: the estimate computes fully while
        arming signals (approval warnings, pace deficit) stay off."""
        db = make_db()
        try:
            add_pool_snap(db, 1, workers={"a": (200.0, 100.0)},
                          extra=self.EXTRA)
            stats, scored = self._supply()
            est = experiment.pool_share_estimate(
                db, self._rentals(),
                dict(CFG, pool_share_enabled=False,
                     pool_share_target_pct=1.0,
                     pool_share_approved_usd=50.0,
                     pool_share_approved_hr=0.40,
                     pool_share_approved_pct=1.0), now=NOW,
                stats=stats, scored=scored, remaining=5.0)
            self.assertFalse(est["enabled"])
            self.assertAlmostEqual(est["needed_its"], 1400.0)
            self.assertAlmostEqual(est["est_cost_hr"], 0.70)
            self.assertAlmostEqual(est["deficit_its"], 700.0)
            self.assertIsNotNone(est["est_cost_epoch"])
            self.assertIsNotNone(est["affordable_pct"])
            # arm-state signals stay off regardless of the numbers
            self.assertFalse(est["reapproval_needed"])
            self.assertFalse(est["pace_deficit"])
            self.assertIsNone(
                experiment._share_price_cap(
                    {"pool_share": est}, CFG))
        finally:
            db.close()

    def test_enabled_flag_validated(self):
        for bad in ("x", 1, 0.0, None):
            with self.assertRaises(ValueError, msg=str(bad)):
                experiment.validate_config(
                    dict(CFG, pool_share_enabled=bad))
        for ok in (True, False):
            self.assertIs(experiment.validate_config(
                dict(CFG, pool_share_enabled=ok))[
                    "pool_share_enabled"], ok)

    def test_target_pct_validated(self):
        for bad in ("x", True, -1, 101, float("nan"), float("inf")):
            with self.assertRaises(ValueError, msg=str(bad)):
                experiment.validate_config(
                    dict(CFG, pool_share_target_pct=bad))
        for ok in (0, 0.5, 100):
            self.assertEqual(experiment.validate_config(
                dict(CFG, pool_share_target_pct=ok))[
                    "pool_share_target_pct"], ok)

    def _supply(self):
        """Three rigs priced under the share cap ($2): A best-eff
        then B then C; est_its resolves via server_prior."""
        stats = {"server_prior": {1: {"its": 800.0},
                                  2: {"its": 600.0},
                                  3: {"its": 500.0}}}
        scored = {
            1: {"hourly_cost_usd": 0.30, "cpu": "c", "gpu": "g",
                "ineligible_reason": None},   # 2667 its/$
            2: {"hourly_cost_usd": 0.40, "cpu": "c", "gpu": "g",
                "ineligible_reason": None},   # 1500
            3: {"hourly_cost_usd": 0.50, "cpu": "c", "gpu": "g",
                "ineligible_reason": None},   # 1000
        }
        return stats, scored

    def test_supply_curve_prices_target(self):
        db = make_db()
        try:
            add_pool_snap(db, 1, workers={"a": (200.0, 100.0)},
                          extra=self.EXTRA)
            stats, scored = self._supply()
            est = experiment.pool_share_estimate(
                db, [], dict(CFG, pool_share_enabled=True,
                             pool_share_target_pct=1.0), now=NOW,
                stats=stats, scored=scored)
            # needed_its = 1400; walk A(800)+B(600) = 1400 -> $0.70/hr
            self.assertEqual(est["cost_basis"], "supply_curve")
            self.assertAlmostEqual(est["est_cost_hr"], 0.70)
            self.assertAlmostEqual(est["cap_ceiling_its"], 1900.0)
            self.assertIsNone(est["supply_short_its"])
        finally:
            db.close()

    def test_supply_shortfall_flagged(self):
        db = make_db()
        try:
            add_pool_snap(db, 1, workers={"a": (200.0, 100.0)},
                          extra=self.EXTRA)
            stats, scored = self._supply()
            est = experiment.pool_share_estimate(
                db, [], dict(CFG, pool_share_enabled=True,
                             pool_share_target_pct=2.0), now=NOW,
                stats=stats, scored=scored)
            # 2% -> 2800 its needed; ceiling 1900 -> short 900
            self.assertIsNone(est["est_cost_hr"])
            self.assertIsNone(est["cost_basis"])
            self.assertAlmostEqual(est["supply_short_its"], 900.0)
            self.assertAlmostEqual(est["cap_ceiling_its"], 1900.0)
        finally:
            db.close()

    def test_deficit_uses_fleet_max(self):
        db = make_db()
        try:
            add_pool_snap(db, 1, workers={"a": (200.0, 100.0)},
                          extra=self.EXTRA)
            stats, scored = self._supply()
            rentals = [{"its_total": 500.0, "hourly_usd": "0.10",
                        "share_arrivals": [("t", 1)]},
                       {"its_total": 200.0, "hourly_usd": "0.10",
                        "share_arrivals": [("t", 1)]}]
            est = experiment.pool_share_estimate(
                db, rentals, dict(CFG, pool_share_enabled=True,
                                  pool_share_target_pct=1.0), now=NOW,
                stats=stats, scored=scored)
            # fleet_its = max(feed 100, measured 700) = 700
            self.assertAlmostEqual(est["fleet_its"], 700.0)
            self.assertAlmostEqual(est["deficit_its"], 700.0)
        finally:
            db.close()

    def test_approved_drift_and_reapproval(self):
        db = make_db()
        try:
            add_pool_snap(db, 1, workers={"a": (200.0, 100.0)},
                          extra=self.EXTRA)
            stats, scored = self._supply()
            base = dict(CFG, pool_share_enabled=True,
                        pool_share_target_pct=1.0,
                        pool_share_approved_usd=50.0,
                        pool_share_approved_hr=0.60,
                        pool_share_approved_pct=1.0,
                        pool_share_approved_at="2026-11-15T10:00")
            est = experiment.pool_share_estimate(
                db, [], base, now=NOW, stats=stats, scored=scored)
            self.assertAlmostEqual(est["approved"]["usd"], 50.0)
            # est 0.70/hr vs approved 0.60 -> +16.7% < 25% drift
            self.assertAlmostEqual(est["cost_drift"], 0.70/0.60 - 1)
            self.assertFalse(est["reapproval_needed"])
            # a target change always forces re-approval
            est2 = experiment.pool_share_estimate(
                db, [], dict(base, pool_share_target_pct=2.0),
                now=NOW, stats=stats, scored=scored)
            self.assertTrue(est2["reapproval_needed"])
            # and cost drift past the band does too
            est3 = experiment.pool_share_estimate(
                db, [], dict(base, pool_share_approved_hr=0.50),
                now=NOW, stats=stats, scored=scored)
            self.assertTrue(est3["reapproval_needed"])
        finally:
            db.close()

    def test_pace_deficit_and_affordable(self):
        db = make_db()
        try:
            add_pool_snap(db, 1, workers={"a": (200.0, 100.0)},
                          extra=self.EXTRA)
            stats, scored = self._supply()
            cfg = dict(CFG, pool_share_enabled=True,
                       pool_share_target_pct=1.0)
            # est_epoch = 0.70/hr x 84h = 58.80; remaining 20 < est
            est = experiment.pool_share_estimate(
                db, [], cfg, now=NOW, stats=stats, scored=scored,
                remaining=20.0)
            self.assertTrue(est["pace_deficit"])
            # remaining/84h = $0.238/hr: buys A(0.30)? no -- every rig
            # exceeds the budget, so affordable_its = 0
            self.assertAlmostEqual(est["affordable_pct"], 0.0)
            est2 = experiment.pool_share_estimate(
                db, [], cfg, now=NOW, stats=stats, scored=scored,
                remaining=100.0)
            self.assertFalse(est2["pace_deficit"])
            # 100/84 = $1.19/hr: A+B+C ($1.20) just misses C -> 1400 its
            # -> 1400 x 2 sol / 280000 = 1.0%
            self.assertAlmostEqual(est2["affordable_pct"], 1.0)
        finally:
            db.close()

    def test_share_price_cap_arming(self):
        cfg = dict(CFG, pool_share_max_price_hr="2.00",
                   max_price_hr="0.50")
        off = {"pool_share": {"enabled": False}}
        self.assertIsNone(experiment._share_price_cap(off, cfg))
        unapproved = {"pool_share": {
            "enabled": True, "deficit_its": 100,
            "approved": {"usd": 0}}}
        self.assertIsNone(
            experiment._share_price_cap(unapproved, cfg))
        caught_up = {"pool_share": {
            "enabled": True, "deficit_its": 0,
            "approved": {"usd": 50}}}
        self.assertIsNone(
            experiment._share_price_cap(caught_up, cfg))
        armed = {"pool_share": {
            "enabled": True, "deficit_its": 10,
            "approved": {"usd": 50}}}
        self.assertEqual(
            experiment._share_price_cap(armed, cfg), Decimal("2.00"))
        # the share cap never lowers the normal cap
        cfg2 = dict(cfg, pool_share_max_price_hr="0.10")
        self.assertEqual(
            experiment._share_price_cap(armed, cfg2), Decimal("0.50"))

    def test_candidates_take_lifted_cap_when_armed(self):
        db = make_db()
        try:
            phase = add_phase(db)
            # $15/day -> $0.656/hr: over max_price_hr ($0.50) but
            # inside the $2 share cap
            add_snapshot(db, [listing(server_id=1, price=15)])
            add_history(db, 1)   # server_prior + cpu/gpu priors
            add_pool_snap(db, 1, workers={"a": (200.0, 100.0)},
                          extra=self.EXTRA)
            cfg = dict(CFG, pool_share_enabled=True,
                       pool_share_target_pct=1.0,
                       pool_share_approved_usd=50.0,
                       pool_share_approved_hr=0.60,
                       pool_share_approved_pct=1.0,
                       pool_share_approved_at=iso(30))
            state = experiment.build_state(
                db, phase, NOW, cfg, say=lambda *_a: None)
            # $1.20/hr is over max_price_hr ($0.50) but inside the
            # share cap; with a deficit it should not be rejected
            rejects = {}
            cands = experiment._candidates(
                state, cfg, set(), [], rejects=rejects)
            self.assertNotIn("over_cap", rejects)
            self.assertEqual(
                [c["server_id"] for c in cands], [1])
        finally:
            db.close()

    def test_approve_pool_share_sets_budget(self):
        db = make_db()
        try:
            cfg = dict(CFG, pool_share_enabled=True,
                       pool_share_target_pct=1.0)
            phase = add_phase(db, budget="10", cfg=cfg)
            # $12/day -> $0.525/hr; server_prior -> 2000 its supply
            add_snapshot(db, [listing(server_id=1, price=12)])
            add_history(db, 1, its_cpu=1500, its_gpu=500)
            add_pool_snap(db, 1, workers={"a": (200.0, 100.0)},
                          extra=self.EXTRA)
            out = experiment.approve_pool_share(db, now=NOW)
            # est 0.525/hr x 84h left = 44.10; committed 0 -> budget
            self.assertAlmostEqual(out["est_cost_epoch"], 44.1)
            self.assertEqual(out["budget_usd"], "44.10")
            row = db.execute(
                "SELECT budget_usd, config_json FROM"
                " experiment_phases WHERE phase_id=?",
                (phase["phase_id"],)).fetchone()
            self.assertEqual(row["budget_usd"], out["budget_usd"])
            saved = json.loads(row["config_json"])
            self.assertAlmostEqual(saved["pool_share_approved_usd"],
                                   out["est_cost_epoch"])
            self.assertEqual(saved["pool_share_approved_pct"], 1.0)
            self.assertTrue(saved["pool_share_approved_at"])
            # re-estimate on the funded phase: pace_deficit clears
            phase2 = db.execute(
                "SELECT * FROM experiment_phases WHERE phase_id=?",
                (phase["phase_id"],)).fetchone()
            cfg2 = experiment.validate_config(
                json.loads(phase2["config_json"]))
            state = experiment.build_state(
                db, dict(phase2), NOW, cfg2, say=lambda *_a: None)
            est = state["pool_share"]
            self.assertFalse(est["pace_deficit"])
            self.assertAlmostEqual(
                est["approved"]["usd"], out["est_cost_epoch"])
            self.assertFalse(est["reapproval_needed"])
        finally:
            db.close()

    def test_approve_pool_share_refuses_no_estimate(self):
        db = make_db()
        try:
            add_phase(db, cfg=dict(CFG, pool_share_enabled=True,
                                   pool_share_target_pct=5.0))
            with self.assertRaises(ValueError):
                experiment.approve_pool_share(db, now=NOW)
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
