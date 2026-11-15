"""tests for tui_data.read_dashboard: Clore order fields folded into
fleet rows and unmanaged orders appearing as minimal fleet rows."""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import experiment  # noqa: E402
import orders  # noqa: E402
import spot  # noqa: E402
import tui_data  # noqa: E402

TS = "2026-11-15T00:00:00+00:00"


def order_record(oid, sid, **kw):
    rec = {"order_id": oid, "server_id": sid, "spot": False,
           "currency": "USD-Blockchain", "price_day_native": 4.8,
           "spend_reported_native": 1.23, "online_reported": True,
           "control": "cli", "gpus": ["RTX 3090"],
           "cpu": "AMD Ryzen 9 7950X",
           "quoted_creation_fee_native": 0.1, "created_unix": 1,
           "rental_end": 9999999999}
    rec.update(kw)
    return rec


def make_db(path):
    db = experiment.open_db(path)
    db.executescript(orders.SCHEMA)
    db.executescript(spot.SCHEMA)
    db.execute(
        "INSERT INTO marketplace_snapshots(captured_at, source,"
        " server_count, raw_json) VALUES (?,?,?,?)",
        (TS, "test", 0, json.dumps({"servers": []})))
    for oid, sid, state in ((400, 40, "ready"),
                            (401, 41, "observe-only"),
                            (403, 43, "ready")):
        db.execute(
            "INSERT INTO fleet_rentals(order_id, server_id, alias,"
            " pool, ssh_key_path, provision_state, created_at,"
            " updated_at) VALUES (?,?,?,'jetski','/k',?,?,?)",
            (oid, sid, f"rig-{oid}", state, TS, TS))
    db.execute(
        "INSERT INTO experiment_rentals(order_id, phase_id, server_id,"
        " tier, reason, x_min, y_min, hourly_usd, creation_fee_usd,"
        " quoted_cost_usd, created_at) VALUES"
        " (400, 1, 40, 'zen', 'fill', 30, 15, '0.10', '0.10', '0.70', ?)",
        (TS,))
    older = [order_record(400, 40), order_record(401, 41),
             order_record(403, 43), order_record(500, 50),
             order_record(501, 51)]
    newer = [order_record(400, 40), order_record(401, 41),
             order_record(403, 43), order_record(500, 50)]
    db.execute(
        "INSERT INTO order_inventory(captured_at, ok, orders_json)"
        " VALUES (?,?,?)",
        ("2026-11-15T00:10:00+00:00", 1, json.dumps(older)))
    db.execute(
        "INSERT INTO order_inventory(captured_at, ok, orders_json)"
        " VALUES (?,?,?)",
        ("2026-11-15T00:20:00+00:00", 1, json.dumps(newer)))
    db.commit()
    return db


class TestFleetCloreFields(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "dash.sqlite3")
        make_db(self.path).close()
        self.dash = tui_data.read_dashboard(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_managed_rig_gets_clore_fields(self):
        rig = next(r for r in self.dash["fleet_all"]
                   if r["order_id"] == 400)
        self.assertEqual(rig["spend_reported_native"], 1.23)
        self.assertIs(rig["online_reported"], True)

    def test_control_labels(self):
        # orders.py's "control" only describes the read-only poll;
        # the dashboard labels who rents each rig
        control = {r["order_id"]: r["control"]
                   for r in self.dash["fleet_all"]}
        self.assertEqual(control[400], "experiment")
        self.assertEqual(control[401], "enrolled")
        self.assertEqual(control[403], "managed")
        self.assertEqual(control[500], "unmanaged")

    def test_active_unmanaged_order_in_fleet(self):
        rig = next(r for r in self.dash["fleet"]
                   if r["order_id"] == 500)
        self.assertTrue(rig["unmanaged"])
        self.assertEqual(rig["health"], "unmanaged")
        self.assertIsNone(rig["created_at"])
        self.assertAlmostEqual(rig["rate_hr_usd"], 4.8 / 24 * 1.05)
        self.assertEqual(rig["gpus"], ["RTX 3090"])
        self.assertNotIn("status", rig)

    def test_ended_unmanaged_only_in_fleet_all(self):
        self.assertNotIn(
            501, {r["order_id"] for r in self.dash["fleet"]})
        rig = next(r for r in self.dash["fleet_all"]
                   if r["order_id"] == 501)
        self.assertTrue(rig["unmanaged"])

    def test_experiment_key_present(self):
        # experiment tables exist (created by open_db) but no phase
        self.assertIsNotNone(self.dash["experiment"])
        self.assertIsNone(self.dash["experiment"]["phase"])
        self.assertIn("experiment_signature", self.dash)


if __name__ == "__main__":
    unittest.main()
