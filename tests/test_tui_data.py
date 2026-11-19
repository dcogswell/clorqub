"""tests for tui_data.read_dashboard: Clore order fields folded into
fleet rows and unmanaged orders appearing as minimal fleet rows."""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import blacklist  # noqa: E402
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
    def test_blacklist_excludes_candidates_and_updates_signature(self):
        db = experiment.open_db(self.path)
        from test_experiment import listing
        db.execute("UPDATE marketplace_snapshots SET raw_json=?",
                   (json.dumps({"servers": [listing(12)]}),))
        db.commit()
        before = tui_data.read_dashboard(self.path)
        blacklist.add(db, 12, "flaky_ssh", "manual")
        after = tui_data.read_dashboard(self.path)
        row = after["rows"][0]
        self.assertFalse(row["eligible"])
        self.assertEqual(row["ineligible_reason"], "blacklisted: flaky_ssh")
        self.assertEqual(row["blacklist"]["source"], "manual")
        self.assertNotEqual(before["blacklist_signature"], after["blacklist_signature"])
        blacklist.remove(db, 12)
        self.assertEqual(tui_data.read_dashboard(self.path)["blacklist_signature"],
                         before["blacklist_signature"])
        db.close()

    def test_experiment_gates_mark_candidates_ineligible(self):
        """The Candidates tab must match the experiment's pool:
        min_reliability and max_price_hr rejections, not just
        score_listing's marketplace checks."""
        db = experiment.open_db(self.path)
        from test_experiment import listing
        db.execute("UPDATE marketplace_snapshots SET raw_json=?",
                   (json.dumps({"servers": [
                       listing(10),                          # ok
                       listing(11, reliability=0.50),        # low rel
                       listing(12, reliability=None),        # unrated
                       listing(13, price=20.0),              # over cap
                   ]}),))
        db.commit()
        rows = {r["server_id"]: r
                for r in tui_data.read_dashboard(self.path)["rows"]}
        db.close()
        self.assertTrue(rows[10]["eligible"])
        self.assertFalse(rows[11]["eligible"])
        self.assertEqual(rows[11]["ineligible_reason"],
                         "reliability < 75%")
        self.assertFalse(rows[12]["eligible"])
        self.assertEqual(rows[12]["ineligible_reason"],
                         "reliability < 75%")
        self.assertFalse(rows[13]["eligible"])
        self.assertEqual(rows[13]["ineligible_reason"],
                         "over $0.50/h cap")

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

    def test_fleet_mode_from_inventory(self):
        """rig.mode maps the last good inventory's spot flag:
        True -> 'spot', False or a missing order -> 'on-demand',
        a present-but-null flag -> None (unknown)."""
        db = experiment.open_db(self.path)
        inv = db.execute(
            "SELECT id, orders_json FROM order_inventory"
            " ORDER BY id DESC LIMIT 1").fetchone()
        recs = json.loads(inv["orders_json"])
        for r in recs:
            if r["order_id"] == 403:
                r["spot"] = True
            elif r["order_id"] == 401:
                r.pop("spot")
        db.execute("UPDATE order_inventory SET orders_json=?"
                   " WHERE id=?", (json.dumps(recs), inv["id"]))
        db.commit()
        db.close()
        modes = {r["order_id"]: r.get("mode")
                 for r in tui_data.read_dashboard(
                     self.path)["fleet_all"]}
        self.assertEqual(modes[403], "spot")
        self.assertIsNone(modes[401])
        self.assertEqual(modes[400], "on-demand")
        self.assertEqual(modes[500], "on-demand")
        # 501 is absent from the latest inventory entirely
        self.assertEqual(modes[501], "on-demand")

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


class RunnerPoolTests(unittest.TestCase):
    """When the experiment exports candidate_values, runner-pool
    membership (not the static gates) decides Candidates rows, and
    the runner's forecasts merge onto them."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "dash.sqlite3")
        make_db(self.path).close()

    def tearDown(self):
        self.tmp.cleanup()

    def _listings(self, *servers):
        db = experiment.open_db(self.path)
        db.execute("UPDATE marketplace_snapshots SET raw_json=?",
                   (json.dumps({"servers": list(servers)}),))
        db.commit()
        db.close()

    def _patch_summary(self, candidate_values):
        return mock.patch.object(
            tui_data.experiment, "summary", return_value={
                "phase": {"phase_id": 1,
                          "value_model": {"source": "prior"}},
                "events": [], "rentals": [],
                "candidate_term_hours": 6,
                "candidate_values": candidate_values})

    def test_runner_pool_membership_and_forecast_merge(self):
        from test_experiment import listing
        self._listings(
            listing(10, gpu="RTX 9999"),   # unbenchmarked, in pool
            listing(11),                   # not in pool
            listing(12, reliability=0.5))  # static-rejected, no pool
        cvs = [{"server_id": 10, "est_its": 123.0,
                "extrapolated": True, "usd_hr": 0.25,
                "return_x": 2.4, "prior": 2.4, "eff_hr": 0.11,
                "hourly": 0.105, "fill_status": "value ok"}]
        with self._patch_summary(cvs):
            dash = tui_data.read_dashboard(self.path)
        rows = {r["server_id"]: r for r in dash["rows"]}
        # pool membership makes the row a candidate, carries the
        # runner's forecast and overrides the local estimate
        self.assertTrue(rows[10]["eligible"])
        self.assertIsNone(rows[10]["ineligible_reason"])
        self.assertEqual(rows[10]["auto_prior"], 2.4)
        self.assertEqual(rows[10]["forecast_usd_hr"], 0.25)
        self.assertEqual(rows[10]["forecast_return_x"], 2.4)
        self.assertEqual(rows[10]["value_fill_status"], "value ok")
        self.assertEqual(rows[10]["est_its"], 123.0)
        self.assertTrue(rows[10]["est_extrapolated"])
        self.assertAlmostEqual(rows[10]["est_its_per_usd"],
                               123.0 / 0.105)
        # outside the pool: stays in the Marketplace; a static reason
        # is kept, otherwise marked not-in-pool
        self.assertFalse(rows[11]["eligible"])
        self.assertEqual(rows[11]["ineligible_reason"],
                         "not in auto pool")
        self.assertNotIn("auto_prior", rows[11])
        self.assertFalse(rows[12]["eligible"])
        self.assertEqual(rows[12]["ineligible_reason"],
                         "reliability < 75%")
        self.assertIsNotNone(dash["candidate_signature"])

    def test_no_phase_keeps_static_fallback(self):
        from test_experiment import listing
        self._listings(listing(10))
        rows = {r["server_id"]: r
                for r in tui_data.read_dashboard(self.path)["rows"]}
        self.assertTrue(rows[10]["eligible"])
        self.assertNotIn("auto_prior", rows[10])
        self.assertIsNone(rows[10].get("forecast_usd_hr"))


if __name__ == "__main__":
    unittest.main()
