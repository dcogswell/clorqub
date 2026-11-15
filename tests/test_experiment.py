"""Tests for experiment.py (auto-rental runner), trees.py and
rig_ssh.restart_miner. All external effects are stubbed: no Clore API
calls, no SSH, dry-run by default."""

import json
import os
import sqlite3
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
import rig_ssh  # noqa: E402
import trees  # noqa: E402

NOW = datetime(2026, 11, 15, 12, 0, 0, tzinfo=timezone.utc)
CFG = dict(experiment.DEFAULT_CONFIG)


def iso(minutes_ago):
    return (NOW - timedelta(minutes=minutes_ago)).isoformat()


def listing(server_id=1, cpu="Intel Core i9-10980XE",
            gpu="RTX 3070", price=2.4):
    return {
        "id": server_id, "owner": 42, "rented": False,
        "cuda_version": "13.0", "gpu_array": [gpu],
        "reliability": 0.95,
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


def add_event(db, phase_id, minutes_ago, kind, order_id=None,
              server_id=None, detail=None):
    db.execute(
        "INSERT INTO experiment_events(ts, phase_id, order_id,"
        " server_id, kind, detail_json) VALUES (?,?,?,?,?,?)",
        (iso(minutes_ago), phase_id, order_id, server_id, kind,
         json.dumps(detail or {})))
    db.commit()


def actions(db, phase, cfg=None):
    state = experiment.build_state(db, phase, NOW, cfg or CFG,
                                   say=lambda *_a: None)
    return experiment.decide(NOW, state, cfg or CFG), state


def kinds(acts, kind):
    return [a for a in acts if a["type"] == kind]


class DecideTests(unittest.TestCase):
    def test_startup_restarts_every_two_min_until_share(self):
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [])
            add_fleet(db, 1, 11)
            add_rental(db, phase["phase_id"], 1, 11, 8)
            for t in (6, 4, 2, 0.5):
                add_obs(db, 1, t, status(submitted=0))
            acts, _ = actions(db, phase)
            self.assertEqual(kinds(acts, "restart"),
                             [{"type": "restart", "order_id": 1,
                               "server_id": 11, "reason": "startup"}])
            # the 2- and 4-min re-rolls happened: the 6-min one is due
            add_event(db, phase["phase_id"], 4, "restart", order_id=1,
                      server_id=11)
            add_event(db, phase["phase_id"], 2, "restart", order_id=1,
                      server_id=11)
            acts, _ = actions(db, phase)
            self.assertEqual(len(kinds(acts, "restart")), 1)
            # a restart < 2 min ago: cadence gate suppresses another
            add_event(db, phase["phase_id"], 0.5, "restart",
                      order_id=1, server_id=11)
            acts, _ = actions(db, phase)
            self.assertEqual(kinds(acts, "restart"), [])
            # once a share arrives, no more startup restarts
            db2 = make_db()
            try:
                phase2 = add_phase(db2)
                add_snapshot(db2, [])
                add_fleet(db2, 1, 11)
                add_rental(db2, phase2["phase_id"], 1, 11, 8)
                add_obs(db2, 1, 3, status(submitted=0))
                add_obs(db2, 1, 0.5, status(submitted=4))
                acts, _ = actions(db2, phase2)
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

    def test_dry_cancels_and_rerents_same_server(self):
        # a tree that stops producing is dead weight; restarts re-roll
        # nothing (the assignment is pool-side per wallet+alias), so the
        # runner cancels and schedules a same-server re-rent instead
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [])
            add_fleet(db, 1, 11)
            add_rental(db, phase["phase_id"], 1, 11, 30)
            add_obs(db, 1, 20, status(submitted=5))
            add_obs(db, 1, 0.5, status(submitted=5))
            acts, _ = actions(db, phase)
            self.assertEqual(kinds(acts, "restart"), [])
            cancels = kinds(acts, "cancel")
            self.assertEqual(len(cancels), 1)
            self.assertEqual(cancels[0]["reason"], "dry")
            self.assertEqual(
                cancels[0]["rerent_at"],
                (NOW + timedelta(minutes=2)).isoformat())
        finally:
            db.close()

    def test_dry_rerent_daily_cap(self):
        # after dry_rerent_max_per_day dry cancels on one server, the
        # runner stops paying fees and lets the rig ride the pool's own
        # rotation instead
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [])
            add_fleet(db, 1, 11)
            add_rental(db, phase["phase_id"], 1, 11, 30)
            add_obs(db, 1, 20, status(submitted=5))
            add_obs(db, 1, 0.5, status(submitted=5))
            for _ in range(CFG["dry_rerent_max_per_day"]):
                add_event(db, phase["phase_id"], 1, "cancel", order_id=1,
                          server_id=11, detail={"reason": "dry"})
            acts, _ = actions(db, phase)
            self.assertEqual(kinds(acts, "cancel"), [])
        finally:
            db.close()

    def test_zero_its_restart_instead_of_dry_rerent(self):
        # a rig hashing nothing has a local fault, not a dead tree;
        # a miner restart can still un-wedge it
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
            self.assertEqual(kinds(acts, "restart"),
                             [{"type": "restart", "order_id": 1,
                               "server_id": 11, "reason": "zero_its"}])
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
            self.assertEqual(
                cancels[0]["rerent_at"],
                (NOW + timedelta(minutes=15)).isoformat())
        finally:
            db.close()

    def test_due_rerent_and_grace_expiry(self):
        db = make_db()
        try:
            phase = add_phase(db)
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
            phase = add_phase(db)
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

    def test_zen_tier_preferred_for_first_half(self):
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [
                listing(server_id=7, cpu="AMD Ryzen 9 7950X "
                                       "16-Core Processor", price=4.8),
                listing(server_id=8, cpu="Intel Core i9-10980XE",
                        price=2.4),
            ])
            acts, _ = actions(db, phase)
            rents = kinds(acts, "rent")
            self.assertEqual(len(rents), 2)
            self.assertEqual(rents[0]["server_id"], 7)
            self.assertEqual(rents[0]["tier"], "zen")
        finally:
            db.close()

    def test_coverage_ordering_prefers_unexplored_hardware(self):
        db = make_db()
        try:
            phase = add_phase(db, cfg=dict(CFG, max_rentals=1))
            old = listing(server_id=42, cpu="Intel Core i9-10980XE",
                          gpu="RTX 3070")
            db.execute(
                "INSERT INTO servers(server_id, owner, country,"
                " first_seen, last_seen, latest_json)"
                " VALUES (?,?,?,?,?,?)",
                (42, 1, "CA", iso(500), iso(0), json.dumps(old)))
            add_fleet(db, 999, 42)
            add_obs(db, 999, 300, status(submitted=1))
            add_obs(db, 999, 240, status(submitted=2))
            db.commit()
            add_snapshot(db, [
                listing(server_id=9, cpu="Intel Core i9-10980XE",
                        gpu="RTX 3070"),
                listing(server_id=10, cpu="Intel Xeon E5-2695 v4",
                        gpu="RTX 3070"),
            ])
            acts, _ = actions(db, phase, cfg=dict(CFG, max_rentals=1))
            rents = kinds(acts, "rent")
            self.assertEqual(len(rents), 1)
            self.assertEqual(rents[0]["server_id"], 10)
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


class ExecutorTests(unittest.TestCase):
    def test_dry_run_makes_no_external_calls(self):
        db = make_db()
        try:
            phase = add_phase(db)
            acts = [
                {"type": "restart", "order_id": 1, "server_id": 11,
                 "reason": "startup"},
                {"type": "cancel", "order_id": 1, "server_id": 11,
                 "reason": "unreachable", "rerent_at": None},
                {"type": "rent", "server_id": 5, "reason": "fill",
                 "tier": "zen"},
            ]
            with mock.patch.object(
                    experiment.rig_ssh, "restart_miner") as ssh_mock, \
                    mock.patch.object(
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
            for m in (ssh_mock, cancel_mock, quote_mock, create_mock,
                      wallet_mock, secrets_mock):
                m.assert_not_called()
            rows = db.execute(
                "SELECT kind FROM experiment_events").fetchall()
            self.assertEqual(len(rows), 3)
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
        add_rental(db, phase["phase_id"], order_id, server_id, 30)
        add_obs(db, order_id, 20, status(submitted=5))
        add_obs(db, order_id, 0.5, status(submitted=5))

    def _cap_dry_rerents(self, db, phase, order_id=1, server_id=11):
        for _ in range(CFG["dry_rerent_max_per_day"]):
            add_event(db, phase["phase_id"], 1, "cancel",
                      order_id=order_id, server_id=server_id,
                      detail={"reason": "dry"})

    def test_auto_burst_only_after_daily_rerent_cap(self):
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [listing(server_id=20)])
            self._dry_slot(db, phase)
            # below the daily cap: cancel + re-rent, no burst
            acts, _ = actions(db, phase)
            self.assertEqual(kinds(acts, "restart"), [])
            self.assertEqual(kinds(acts, "cancel")[0]["reason"], "dry")
            self.assertEqual(kinds(acts, "burst"), [])
            # at the cap: a dry slot triggers a burst instead
            self._cap_dry_rerents(db, phase)
            acts, _ = actions(db, phase)
            bursts = kinds(acts, "burst")
            self.assertEqual(len(bursts), 1)
            self.assertEqual(bursts[0]["trigger"], "dry")
            self.assertEqual(bursts[0]["trigger_order_id"], 1)
            self.assertEqual(bursts[0]["n"], CFG["burst_max"])
            # an unjudged burst blocks new bursts
            add_burst(db, phase["phase_id"], 2)
            acts, _ = actions(db, phase)
            self.assertEqual(kinds(acts, "burst"), [])
            # a judged burst inside the cooldown blocks too
            db2 = make_db()
            try:
                phase2 = add_phase(db2)
                add_snapshot(db2, [listing(server_id=20)])
                self._dry_slot(db2, phase2)
                self._cap_dry_rerents(db2, phase2)
                add_burst(db2, phase2["phase_id"], 30, judged=True)
                acts, _ = actions(db2, phase2)
                self.assertEqual(kinds(acts, "cancel"), [])
                self.assertEqual(kinds(acts, "burst"), [])
                # past the cooldown: burst again
                db2.execute(
                    "UPDATE experiment_bursts SET started_at=?",
                    (iso(61),))
                db2.commit()
                acts, _ = actions(db2, phase2)
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

    def test_probe_renting_one_per_cycle_cheapest(self):
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
            self.assertEqual(probes[0]["hours"], CFG["burst_hours"])
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

    def test_judging_keeps_finder_and_control(self):
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [])
            burst_id = add_burst(db, phase["phase_id"], 10, n_target=3)
            add_fleet(db, 10, 20)
            add_fleet(db, 11, 21)
            add_fleet(db, 12, 22)
            add_rental(db, phase["phase_id"], 10, 20, 7, role="probe",
                       burst_id=burst_id, hourly="0.15")
            add_rental(db, phase["phase_id"], 11, 21, 7, role="probe",
                       burst_id=burst_id, hourly="0.05")
            add_rental(db, phase["phase_id"], 12, 22, 7, role="probe",
                       burst_id=burst_id, hourly="0.10")
            add_obs(db, 10, 6, status(submitted=1))   # finder
            add_obs(db, 10, 0.5, status(submitted=3))
            add_obs(db, 11, 6, status(submitted=0))
            add_obs(db, 11, 0.5, status(submitted=0))
            add_obs(db, 12, 6, status(submitted=0))
            add_obs(db, 12, 0.5, status(submitted=0))
            acts, _ = actions(db, phase)
            judges = kinds(acts, "judge")
            self.assertEqual(len(judges), 1)
            judge = judges[0]
            self.assertEqual(judge["burst_id"], burst_id)
            self.assertEqual(judge["finders"], [10])
            self.assertEqual(judge["keep"], [10])
            self.assertEqual(judge["control"], 11)
            self.assertEqual(judge["cancel"], [12])
            with mock.patch.object(
                    experiment.rent, "cancel_managed_order",
                    return_value=(0, {})) as cancel_mock:
                experiment.execute(
                    db, ":memory:", "/tmp/env", phase, CFG, [judge],
                    NOW, live=True, say=lambda *_a: None)
            cancel_mock.assert_called_once()
            roles = {r["order_id"]: (r["role"], r["end_reason"])
                     for r in db.execute(
                         "SELECT order_id, role, end_reason FROM "
                         "experiment_rentals")}
            self.assertEqual(roles[10][0], "kept")
            self.assertEqual(roles[11][0], "control")
            self.assertEqual(roles[12], ("probe", "probe_no_share"))
            burst = db.execute(
                "SELECT judged_at FROM experiment_bursts WHERE "
                "burst_id=?", (burst_id,)).fetchone()
            self.assertIsNotNone(burst["judged_at"])
            event = db.execute(
                "SELECT detail_json FROM experiment_events WHERE "
                "kind='burst_judged'").fetchone()
            self.assertEqual(json.loads(event["detail_json"])
                             ["cancelled"], [12])
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
            # age 8.5 min: inside [cancel_by-1, cancel_by) maturity
            # band it would judge, but past deadline it must not be
            # kept or judged-cancelled.
            add_rental(db, phase["phase_id"], 10, 20, 8.5,
                       role="probe", burst_id=burst_id)
            add_obs(db, 10, 6, status(submitted=1))
            add_obs(db, 10, 0.5, status(submitted=2))
            acts, _ = actions(db, phase)
            cancels = kinds(acts, "cancel")
            self.assertEqual([c["reason"] for c in cancels],
                             ["probe_deadline"])
            judges = kinds(acts, "judge")
            self.assertEqual(len(judges), 1)
            self.assertNotIn(10, judges[0]["keep"])
            self.assertNotIn(10, judges[0]["cancel"])
            self.assertNotEqual(judges[0]["control"], 10)
        finally:
            db.close()

    def test_probe_guard_blocks_rents(self):
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [listing(server_id=20)])
            burst_id = add_burst(db, phase["phase_id"], 1, n_target=3)
            add_fleet(db, 10, 30)
            # 4.5 min is past cancel_by(8) - PROBE_RENT_GUARD_MIN(4)
            add_rental(db, phase["phase_id"], 10, 30, 4.5,
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
            self.assertEqual(judges[0]["keep"], [])
            self.assertIsNone(judges[0]["control"])
            self.assertEqual(judges[0]["cancel"], [])
        finally:
            db.close()

    def test_probe_cancel_by_must_be_under_free_window(self):
        with self.assertRaises(ValueError):
            experiment.validate_config(
                dict(CFG, probe_cancel_by_min=CFG["free_window_min"]))
        with self.assertRaises(ValueError):
            experiment.validate_config(
                dict(CFG, probe_cancel_by_min=99))

    def test_dry_run_burst_makes_no_calls(self):
        db = make_db()
        try:
            phase = add_phase(db)
            acts = [
                {"type": "burst", "trigger": "manual",
                 "trigger_order_id": None, "n": 4},
                {"type": "judge", "burst_id": 1, "finders": [10],
                 "keep": [10], "control": 11, "cancel": [12]},
                {"type": "rent", "server_id": 5, "reason": "probe",
                 "tier": "other", "role": "probe", "burst_id": 1,
                 "hours": 1},
            ]
            with mock.patch.object(
                    experiment.rig_ssh, "restart_miner") as ssh_mock, \
                    mock.patch.object(
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
            for m in (ssh_mock, cancel_mock, quote_mock, create_mock,
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
                for role, hours in (("slot", None), ("probe", 1)):
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
            self.assertEqual(probe_call.kwargs["hours"],
                             CFG["burst_hours"])
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
        r = _next_step_r(role="probe",
                         created_at=NOW - timedelta(minutes=2),
                         t0=NOW - timedelta(minutes=1, seconds=54))
        self.assertEqual(experiment.next_step(r, NOW, CFG),
                         "judged in 3m")
        r = _next_step_r(role="probe",
                         created_at=NOW - timedelta(minutes=2))
        self.assertEqual(experiment.next_step(r, NOW, CFG),
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
                         "startup restart in now")

    def test_mining_dry_and_gives_up(self):
        r = _next_step_r(t0=NOW - timedelta(minutes=20),
                         last_share_at=NOW - timedelta(minutes=2),
                         quiet_since=NOW - timedelta(minutes=2))
        self.assertEqual(experiment.next_step(r, NOW, CFG),
                         "mining (last share 2m ago)")
        r = _next_step_r(t0=NOW - timedelta(minutes=20),
                         quiet_since=NOW - timedelta(minutes=20))
        self.assertEqual(experiment.next_step(r, NOW, CFG),
                         "re-rent in now")
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
                        "shares", "restarts", "tree_id", "x_min",
                        "y_min", "next"):
                self.assertIn(key, rental)
            self.assertEqual(rental["order_id"], 10)
            self.assertEqual(rental["role"], "slot")
            self.assertEqual(rental["shares"], 5)
            self.assertEqual(rental["restarts"], 1)
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


class AutoBlacklistTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db()
        self.addCleanup(self.db.close)
        self.phase = add_phase(self.db)
        add_snapshot(self.db, [listing(11), listing(12)])
        add_fleet(self.db, 1, 11)
        add_rental(self.db, self.phase["phase_id"], 1, 11, 40)

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
        st["hardware"]["nproc"] = 18
        self.assertIsNone(experiment.spec_mismatch(listing(), st))
        self.assertIsNone(experiment.spec_mismatch(listing(), status()))

    def test_low_its_requires_independent_compatible_rentals(self):
        def measured(cpu=10, gpu=80, epoch=239):
            st = status(0, cpu=cpu, gpu=gpu)
            st.update(pool="jetski", package={"version": "v1"})
            st["hardware"]["nproc"] = 18
            st["shares"]["epoch"] = epoch
            return st
        known = {11: listing(11), 12: listing(12)}
        for minute in range(40, -1, -1):
            add_obs(self.db, 1, minute, measured(cpu=2))
        with mock.patch.object(experiment, "server_listings", return_value=known):
            self.assertEqual(self.check(), [])
            for oid in (2, 3, 4):
                add_fleet(self.db, oid, 12)
                for minute in (40, 30, 20, 10, 0):
                    add_obs(self.db, oid, minute, measured())
                if oid < 4:
                    self.assertEqual(self.check(), [])
            self.assertEqual(self.check()[0][2], "low_its:cpu")
            self.assertEqual(self.check()[0][3]["n_rentals"], 3)
            # A previous algorithm is not a valid prior.
            self.db.execute("UPDATE rig_observations SET status_json=? "
                            "WHERE order_id=4", (json.dumps(measured(epoch=238)),))
            self.db.commit()
            self.assertEqual(self.check(), [])
            self.db.execute("UPDATE rig_observations SET status_json=? "
                            "WHERE order_id=4", (json.dumps(measured()),))
            self.db.execute("UPDATE rig_observations SET status_json=? "
                            "WHERE order_id=1", (json.dumps(measured(gpu=10)),))
            self.db.commit()
            self.assertEqual(self.check()[0][2], "low_its:gpu")

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
        self.assertEqual(experiment._probe_candidates(state, CFG, {11}, []), [])
        add_event(self.db, self.phase["phase_id"], 20, "rerent_scheduled",
                  server_id=12, detail={"server_id": 12, "tier": "other",
                                        "due_at": iso(1)})
        acts, _ = actions(self.db, self.phase)
        self.assertNotIn(12, [a["server_id"] for a in kinds(acts, "rent")])


if __name__ == "__main__":
    unittest.main()
