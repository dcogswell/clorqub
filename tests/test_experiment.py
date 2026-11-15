"""Tests for experiment.py (auto-rental runner), trees.py and
rig_ssh.restart_miner. All external effects are stubbed: no Clore API
calls, no SSH, dry-run by default."""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

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
               x_min=30, y_min=15, tier="other", reason="fill"):
    db.execute(
        "INSERT INTO experiment_rentals(order_id, phase_id, server_id,"
        " tier, reason, x_min, y_min, hourly_usd, creation_fee_usd,"
        " quoted_cost_usd, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (order_id, phase_id, server_id, tier, reason, x_min, y_min,
         "0.10", "0.10", "0.70", iso(created_min)))
    db.commit()


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

    def test_dry_restart_and_cooldown(self):
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [])
            add_fleet(db, 1, 11)
            add_rental(db, phase["phase_id"], 1, 11, 30)
            add_obs(db, 1, 20, status(submitted=5))
            add_obs(db, 1, 0.5, status(submitted=5))
            acts, _ = actions(db, phase)
            self.assertEqual(kinds(acts, "restart"),
                             [{"type": "restart", "order_id": 1,
                               "server_id": 11, "reason": "dry"}])
            # a restart 5 min ago is inside the 10 min cooldown
            add_event(db, phase["phase_id"], 5, "restart", order_id=1,
                      server_id=11)
            acts, _ = actions(db, phase)
            self.assertEqual(kinds(acts, "restart"), [])
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


if __name__ == "__main__":
    unittest.main()
