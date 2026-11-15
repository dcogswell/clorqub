"""Tests for the read-only marketplace scanner and hardware reference."""

import json
import os
import sqlite3
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import market  # noqa: E402


def listing(server_id=1, gpu="RTX 3070", cpu="AMD Ryzen 9 7950X",
            country="CA", price=2.4, rented=False, cuda="13.0"):
    return {
        "id": server_id,
        "owner": 42,
        "rented": rented,
        "cuda_version": cuda,
        "gpu_array": [gpu],
        "reliability": 0.95,
        "price": {"usd": {"on_demand_usd": price, "spot": 0}},
        "specs": {
            "cpu": cpu,
            "cpus": "16/32",
            "gpuram": 8,
            "ram": 64,
            "net": {"cc": country},
        },
    }


class TestMarketplaceScoring(unittest.TestCase):
    def test_user_anchor_matches_7945hx_and_applies_fee(self):
        row = market.score_listing(
            listing(cpu="AMD Ryzen 9 7945HX 16-Core Processor")
        )
        self.assertTrue(row["eligible"])
        self.assertEqual(row["estimated_sols_hr"], 160)
        self.assertAlmostEqual(row["effective_price_day_usd"], 2.52)
        self.assertAlmostEqual(row["estimated_sols_per_usd"], 160 / (2.52 / 24))
        self.assertEqual(row["cpu_isa"], "AVX512_inferred")

    def test_avx2_has_no_fabricated_yield_prior(self):
        row = market.score_listing(listing(cpu="AMD Ryzen 9 3900"))
        self.assertFalse(row["eligible"])
        self.assertEqual(row["ineligible_reason"], "no matching benchmark")
        self.assertIsNone(row["estimated_sols_hr"])

    def test_only_reference_cpu_models_get_a_yield_prior(self):
        for cpu in ("AMD Ryzen 9 7950X3D", "AMD Ryzen 9 7900X",
                    "AMD Ryzen 9 9950X", "AMD EPYC 9654",
                    "Intel(R) Xeon(R) Platinum 8272CL"):
            with self.subTest(cpu=cpu):
                row = market.score_listing(listing(cpu=cpu))
                self.assertFalse(row["eligible"])
                self.assertIsNone(row["estimated_sols_hr"])
                self.assertEqual(row["cpu_isa"], "AVX512_inferred")
        self.assertNotEqual(market.cpu_profile("AMD EPYC 7763")[1], "AVX512_inferred")

    def test_tentative_iteration_rates_are_not_used_in_score(self):
        row = market.score_listing(listing())
        self.assertEqual(row["estimated_sols_hr"], 160)
        self.assertEqual(row["benchmark_kind"], "approximate")
        self.assertFalse(row["spend_approved"])
        self.assertIn("allocation", row["reference_uncertainty"])

    def test_ti_yield_is_a_conservative_floor(self):
        row = market.score_listing(listing(gpu="RTX 3090 Ti"))
        self.assertEqual(row["estimated_sols_hr"], 350)
        self.assertIn("lower bound", row["benchmark_notes"])
        self.assertEqual(row["benchmark_kind"], "lower_bound")

    def test_country_cuda_and_multi_gpu_filters(self):
        self.assertEqual(
            market.score_listing(listing(country="RU"))["ineligible_reason"],
            "blocked country",
        )
        self.assertEqual(
            market.score_listing(listing(cuda="11.7"))["ineligible_reason"],
            "CUDA unknown or below 12.0",
        )
        multi = listing()
        multi["gpu_array"] = ["RTX 3070", "RTX 3070"]
        self.assertEqual(
            market.score_listing(multi)["ineligible_reason"],
            "not a single-GPU listing",
        )

    def test_spot_zero_placeholder_is_not_scored(self):
        row = market.score_listing(listing(), mode="spot")
        self.assertFalse(row["eligible"])
        self.assertEqual(row["ineligible_reason"], "no supported USD price")

    def test_invalid_or_disabled_usd_quotes_are_unscored(self):
        for value in (0, -1, None, "unknown", "nan", "inf", True):
            with self.subTest(value=value):
                row = market.score_listing(listing(price=value))
                self.assertFalse(row["eligible"])
                self.assertIsNone(row["estimated_sols_per_usd"])
        disabled = listing()
        disabled["allowed_coins"] = ["bitcoin"]
        self.assertIsNone(market.score_listing(disabled)["hourly_cost_usd"])

    def test_laptop_gpu_does_not_use_desktop_benchmark(self):
        row = market.score_listing(listing(gpu="NVIDIA GeForce RTX 3070 Laptop GPU"))
        self.assertFalse(row["eligible"])

    def test_excluded_listings_are_not_eligible(self):
        partial = listing()
        partial["partial_gpu_rental"] = {"available_gpus": 0}
        for server in (listing(rented=True), listing(cuda=None), partial):
            self.assertFalse(market.score_listing(server)["eligible"])

    def test_other_pool_benchmarks_are_not_applied_to_jetski(self):
        rows = [dict(market.USER_BENCHMARKS[0], pool="qli")]
        self.assertIsNone(
            market.score_listing(listing(), benchmarks=rows)["estimated_sols_hr"]
        )


class TestMarketplaceStorage(unittest.TestCase):
    def test_snapshots_specs_prices_and_benchmarks_persist(self):
        db = market.connect_db(":memory:")
        try:
            first = listing()
            market.ingest_marketplace(
                db, {"code": 0, "servers": [first]}, captured_at="2026-11-14T00:00:00Z"
            )
            changed = listing()
            changed["specs"]["cpu"] = "AMD Ryzen 9 7950X3D"
            changed["price"]["usd"]["on_demand_usd"] = 3.0
            market.ingest_marketplace(
                db, {"code": 0, "servers": [changed]}, captured_at="2026-11-14T01:00:00Z"
            )
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM marketplace_snapshots").fetchone()[0], 2
            )
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM server_specs").fetchone()[0], 2
            )
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM server_prices").fetchone()[0], 2
            )
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM performance_benchmarks").fetchone()[0], 3
            )
            self.assertEqual(
                db.execute("SELECT owner FROM servers WHERE server_id=1").fetchone()[0], 42
            )
            self.assertIsNone(
                db.execute("SELECT spot_usd_day FROM server_prices LIMIT 1").fetchone()[0]
            )
            observations = {
                row["observation_id"]: json.loads(row["raw_json"])
                for row in db.execute("SELECT * FROM performance_observations")
            }
            self.assertEqual(observations["user-3070-gpu-rate"]["its"], 100)
            self.assertEqual(observations["user-3070-gpu-rate"]["qualifier"], "tentative")
            self.assertIsNone(observations["user-unspecified-gpu-rate"]["gpu_model"])
        finally:
            db.close()

    def test_spec_history_keeps_gpu_cuda_changes_and_return_to_old_spec(self):
        db = market.connect_db(":memory:")
        try:
            versions = [
                listing(),
                listing(gpu="RTX 3080 Ti"),
                listing(gpu="RTX 3080 Ti", cuda="12.4"),
                listing(),
                listing(),
            ]
            for index, server in enumerate(versions):
                market.ingest_marketplace(
                    db, {"servers": [server]}, captured_at=f"2026-11-14T0{index}:00:00Z"
                )
            self.assertEqual(db.execute("SELECT COUNT(*) FROM server_specs").fetchone()[0], 4)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM server_specs WHERE valid_to IS NULL"
            ).fetchone()[0], 1)
            # Unchanged prices should not be repeated for every scan.
            self.assertEqual(db.execute("SELECT COUNT(*) FROM server_prices").fetchone()[0], 1)
        finally:
            db.close()

    def test_bad_import_does_not_leave_partial_history(self):
        db = market.connect_db(":memory:")
        try:
            bad = listing(server_id=2)
            bad["specs"]["ram"] = ["not a scalar"]
            with self.assertRaises(sqlite3.ProgrammingError):
                market.ingest_marketplace(db, {"servers": [listing(), bad]})
            self.assertEqual(db.execute("SELECT COUNT(*) FROM servers").fetchone()[0], 0)
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM marketplace_snapshots").fetchone()[0], 0
            )
        finally:
            db.close()

    def test_marketplace_payload_must_have_servers(self):
        db = market.connect_db(":memory:")
        try:
            with self.assertRaises(ValueError):
                market.ingest_marketplace(db, {})
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
