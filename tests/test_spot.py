"""Tests for dynamic spot-vs-on-demand selection: spot.bid_for,
rental_policy.prepare_order(mode="spot"), rent._term_matches mode
handling, and experiment ranking/outbid lifecycle. No network."""

import json
import os
import sys
import unittest
from datetime import timedelta
from decimal import Decimal
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cli  # noqa: E402
import experiment  # noqa: E402
import orders  # noqa: E402
import blacklist  # noqa: E402
import rental_policy  # noqa: E402
import rent  # noqa: E402
import spot  # noqa: E402
import telemetry  # noqa: E402
from test_experiment import (NOW, CFG, iso, listing, make_db,  # noqa: E402
                             add_phase, add_snapshot, add_fleet,
                             add_rental, add_history, add_obs,
                             add_burst, actions, kinds, status)

FEES = {
    "code": 0,
    "renter_fees": {
        "on_demand": {"base": {"USD-Blockchain": 5},
                      "extra": {"USD-Blockchain": 0}},
        "spot": {"base": {"USD-Blockchain": 1.25},
                 "extra": {"USD-Blockchain": 0}},
    },
    "creation_fees": {
        "on_demand": {"USD-Blockchain": "0.10"},
        "spot": {"USD-Blockchain": "0.005"},
    },
}

PUBKEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeKeyForTestsOnly x"


def api_server(server_id=7, price=4.8, mrl=24):
    """Raw Clore marketplace server shape (price.on_demand, mrl)."""
    return {
        "id": server_id, "rented": False, "online": True,
        "cuda_version": "13.0", "gpu_array": ["RTX 3070"],
        "reliability": 0.98, "mrl": mrl,
        "allowed_coins": ["USD-Blockchain"],
        "price": {"on_demand": {"USD-Blockchain": price}},
        "specs": {"cpu": "Intel Core i9-10980XE", "cpus": "18/36",
                  "ram": 64, "net": {"cc": "CA"}},
    }


def quote_record(floor=3.6, offers=(), minutes_ago=0, **over):
    payload = {"code": 0, "exists": True,
               "market": {"server": {
                   "min_pricing": {"USD-Blockchain": floor},
                   "online": True, "visibility": "public"},
                   "offers": list(offers)}}
    rec = {"server_id": 7, "captured_at": iso(minutes_ago),
           "raw_json": json.dumps(payload), "error_kind": None}
    rec.update(over)
    return rec


def offer(bid, currency="USD-Blockchain", active=True, mine=False):
    return {"bid": bid, "currency": currency,
            "active": active, "my": mine}


class BidForTests(unittest.TestCase):
    def test_no_offers_bids_floor(self):
        bid = spot.bid_for(quote_record(floor=3.6), NOW)
        self.assertEqual(bid["bid_usd_day"], 3.6)
        self.assertEqual(bid["floor_usd_day"], 3.6)
        self.assertIsNone(bid["top_bid_usd_day"])
        self.assertEqual(bid["offers"], 0)

    def test_competing_usd_bid_rounds_up(self):
        bid = spot.bid_for(
            quote_record(floor=3.0, offers=[offer(3.20)]), NOW)
        self.assertEqual(bid["top_bid_usd_day"], 3.20)
        # 3.20 * 1.01 = 3.232 -> 3.24
        self.assertEqual(bid["bid_usd_day"], 3.24)

    def test_top_below_floor_stays_floor(self):
        bid = spot.bid_for(
            quote_record(floor=3.6, offers=[offer(1.0)]), NOW)
        self.assertEqual(bid["bid_usd_day"], 3.6)

    def test_non_usd_offers_ignored(self):
        bid = spot.bid_for(
            quote_record(floor=3.6, offers=[
                offer(99.0, currency="BTC"), offer(0.5)]), NOW)
        self.assertEqual(bid["bid_usd_day"], max(3.6, 0.5 * 1.01))
        self.assertEqual(bid["offers"], 1)

    def test_own_offer_not_competing(self):
        bid = spot.bid_for(
            quote_record(floor=3.6, offers=[offer(8.0, mine=True)]), NOW)
        self.assertIsNone(bid["top_bid_usd_day"])
        self.assertEqual(bid["bid_usd_day"], 3.6)

    def test_queued_offer_still_sets_the_bar(self):
        bid = spot.bid_for(
            quote_record(floor=3.6, offers=[
                offer(4.0), offer(9.0, active=False)]), NOW)
        self.assertEqual(bid["top_bid_usd_day"], 9.0)
        self.assertEqual(bid["bid_usd_day"], 9.09)

    def test_stale_quote_unusable(self):
        self.assertIsNone(
            spot.bid_for(quote_record(minutes_ago=20), NOW))

    def test_offline_quote_unusable(self):
        rec = quote_record()
        payload = json.loads(rec["raw_json"])
        payload["market"]["server"]["online"] = False
        rec["raw_json"] = json.dumps(payload)
        self.assertIsNone(spot.bid_for(rec, NOW))

    def test_missing_record_unusable(self):
        self.assertIsNone(spot.bid_for(None, NOW))
        self.assertIsNone(spot.bid_for(
            quote_record(error_kind="HTTP 429"), NOW))

    def test_no_floor_unusable(self):
        rec = quote_record()
        payload = json.loads(rec["raw_json"])
        del payload["market"]["server"]["min_pricing"]["USD-Blockchain"]
        rec["raw_json"] = json.dumps(payload)
        self.assertIsNone(spot.bid_for(rec, NOW))


class PrepareOrderSpotTests(unittest.TestCase):
    def _quote(self, **kw):
        args = dict(hours=4, max_cost_usd="50.00", pool="jetski",
                    public_key=PUBKEY, alias="rig-fox-ab12")
        args.update(kw)
        return rental_policy.prepare_order(
            api_server(), FEES, **args)

    def test_spot_payload_shape(self):
        q = self._quote(mode="spot", spot_bid_usd_day=3.6,
                        spot_floor_usd_day=3.6)
        p = q["payload"]
        self.assertEqual(p["type"], "spot")
        self.assertEqual(p["spotprice"], 3.6)
        for absent in ("required_price", "rental_hours", "auto_renew"):
            self.assertNotIn(absent, p)
        self.assertEqual(q["mode"], "spot")
        self.assertEqual(q["spot_bid_usd_day"], "3.6")
        self.assertEqual(q["spot_floor_usd_day"], "3.6")

    def test_spot_fee_schedule(self):
        q = self._quote(mode="spot", spot_bid_usd_day=2.4)
        # 2.4 * 1.0125 / 24
        self.assertAlmostEqual(float(q["hourly_cost_usd"]),
                               2.4 * 1.0125 / 24, places=8)
        self.assertEqual(q["creation_fee_usd"], "0.005")
        bound = Decimal("2.4") * Decimal("1.0125") / 24 * 4 \
            + Decimal("0.005") + rental_policy.COST_MARGIN_USD
        self.assertEqual(q["cost_bound_usd"],
                         str(bound.quantize(Decimal("0.01"))))

    def test_spot_bid_below_floor_rejected(self):
        with self.assertRaises(ValueError):
            self._quote(mode="spot", spot_bid_usd_day=3.0,
                        spot_floor_usd_day=3.6)

    def test_spot_requires_bid(self):
        with self.assertRaises(ValueError):
            self._quote(mode="spot")

    def test_mrl_skipped_for_spot_but_not_on_demand(self):
        short = api_server(mrl=2)
        args = dict(hours=4, max_cost_usd="50.00", pool="jetski",
                    public_key=PUBKEY, alias="rig-fox-ab12")
        q = rental_policy.prepare_order(
            short, FEES, mode="spot", spot_bid_usd_day=3.6, **args)
        self.assertEqual(q["mode"], "spot")
        with self.assertRaises(ValueError):
            rental_policy.prepare_order(short, FEES, **args)

    def test_on_demand_unchanged(self):
        q = self._quote()
        self.assertEqual(q["mode"], "on-demand")
        p = q["payload"]
        self.assertEqual(p["type"], "on-demand")
        self.assertEqual(p["rental_hours"], 4)
        self.assertIn("auto_renew", p)
        self.assertIn("required_price", p)
        self.assertNotIn("spot_bid_usd_day", q)


class TermMatchesSpotTests(unittest.TestCase):
    RECEIPT = {"id": 5, "si": 7, "ct": 1_700_000_000,
               "spot": True}

    def test_spot_receipt_accepted_as_spot(self):
        self.assertTrue(rent._term_matches(self.RECEIPT, 4,
                                           mode="spot"))

    def test_spot_receipt_rejected_as_on_demand(self):
        self.assertFalse(rent._term_matches(self.RECEIPT, 4))

    def test_spot_renewal_rejected(self):
        order = dict(self.RECEIPT, auto_renew=True)
        self.assertFalse(rent._term_matches(order, 4, mode="spot"))

    def test_on_demand_receipt_still_checked(self):
        order = {"ct": 1000, "rental_hours": 4,
                 "rental_end": 1000 + 4 * 3600, "spot": False}
        self.assertTrue(rent._term_matches(order, 4))
        self.assertFalse(rent._term_matches(order, 4, mode="spot"))
        self.assertFalse(rent._term_matches(order, 5))


class SpotSelectionTests(unittest.TestCase):
    """_candidates mode choice driven by cached spot_quotes."""

    def _state(self, db, phase, cfg, floor=1.8, offers=(), sid=5):
        db.executescript(spot.SCHEMA)
        if not db.execute(
                "SELECT 1 FROM servers WHERE server_id=?",
                (sid,)).fetchone():
            db.execute(
                "INSERT INTO servers(server_id, owner, country,"
                " first_seen, last_seen, latest_json)"
                " VALUES (?,?,?,?,?,?)",
                (sid, 1, "CA", iso(500), iso(0),
                 json.dumps(listing(server_id=sid))))
        db.execute(
            "INSERT INTO spot_quotes(server_id, captured_at, raw_json,"
            " error_kind) VALUES (?,?,?,?)",
            (sid, iso(1), quote_record(floor=floor, offers=offers,
                                       minutes_ago=1)["raw_json"],
             None))
        db.commit()
        return experiment.build_state(
            db, phase, NOW, cfg, say=lambda *_a: None)

    def _spot_cfg(self, **kw):
        return dict(CFG, spot_enabled=True, **kw)

    def test_discounted_listing_picks_spot(self):
        db = make_db()
        try:
            cfg = self._spot_cfg()
            phase = add_phase(db, cfg=cfg)
            # listing price 2.4/day -> od hourly 0.105; floor 1.8/day
            # -> spot hourly ~0.0759 (~28% off)
            add_snapshot(db, [listing(server_id=5)])
            state = self._state(db, phase, cfg)
            cands = experiment._candidates(state, cfg, set(), [])
            self.assertEqual(len(cands), 1)
            c = cands[0]
            self.assertEqual(c["mode"], "spot")
            self.assertAlmostEqual(c["spot_hourly"],
                                   1.8 / 24 * 1.0125, places=8)
            self.assertAlmostEqual(c["hourly"], c["spot_hourly"])
            self.assertEqual(c["creation_fee"],
                             experiment._SPOT_CREATION_FEE_USD)
            self.assertAlmostEqual(
                c["eff_hr"],
                c["spot_hourly"] + 0.005 / CFG["hours"], places=8)
            self.assertEqual(c["spot_bid_usd_day"], 1.8)
        finally:
            db.close()

    def test_undiscounted_listing_stays_on_demand(self):
        db = make_db()
        try:
            cfg = self._spot_cfg()
            phase = add_phase(db, cfg=cfg)
            add_snapshot(db, [listing(server_id=5)])
            # floor == list price: 2.4/day -> 0.1012/hr, under 10% off
            state = self._state(db, phase, cfg, floor=2.4)
            c = experiment._candidates(state, cfg, set(), [])[0]
            self.assertEqual(c["mode"], "on-demand")
            self.assertEqual(c["creation_fee"],
                             experiment._CREATION_FEE_USD)
            self.assertIsNone(c["spot_bid_usd_day"])
        finally:
            db.close()

    def test_disabled_ignores_quotes(self):
        db = make_db()
        try:
            phase = add_phase(db, cfg=CFG)
            add_snapshot(db, [listing(server_id=5)])
            state = self._state(db, phase, CFG)
            self.assertEqual(state["spot_quotes"], {})
            c = experiment._candidates(state, CFG, set(), [])[0]
            self.assertEqual(c["mode"], "on-demand")
        finally:
            db.close()

    def test_missing_quote_stays_on_demand(self):
        db = make_db()
        try:
            cfg = self._spot_cfg()
            phase = add_phase(db, cfg=cfg)
            add_snapshot(db, [listing(server_id=5)])
            db.executescript(spot.SCHEMA)
            db.commit()
            state = experiment.build_state(
                db, phase, NOW, cfg, say=lambda *_a: None)
            c = experiment._candidates(state, cfg, set(), [])[0]
            self.assertEqual(c["mode"], "on-demand")
            self.assertIsNone(c["spot_hourly"])
        finally:
            db.close()

    def test_outbid_cools_spot_only(self):
        db = make_db()
        try:
            cfg = self._spot_cfg(spot_outbid_min=60)
            phase = add_phase(db, cfg=cfg)
            add_snapshot(db, [listing(server_id=5)])
            add_fleet(db, 90, 5, state="missing")
            add_rental(db, phase["phase_id"], 90, 5, 40,
                       hourly="0.08")
            db.execute("UPDATE experiment_rentals SET mode='spot'"
                       " WHERE order_id=90")
            db.commit()
            state = self._state(db, phase, cfg)
            self.assertIn(5, state["spot_outbid_cooldown"])
            c = experiment._candidates(state, cfg, set(), [])[0]
            self.assertEqual(c["mode"], "on-demand")
            # still a candidate: on-demand eligibility is unaffected
            self.assertIsNotNone(c["hourly"])
            # the ending itself was classified outbid, not missing
            row = db.execute(
                "SELECT end_reason FROM experiment_rentals"
                " WHERE order_id=90").fetchone()
            self.assertEqual(row["end_reason"], "outbid")
            # and it never blacklists nor feeds the lull streak
            self.assertNotIn(5, state["excluded"])
            self.assertEqual(state["lull_streak"], 0)
        finally:
            db.close()

    def test_probe_threshold_looser_than_slot(self):
        db = make_db()
        try:
            cfg = self._spot_cfg(spot_min_discount=0.30,
                                 spot_probe_min_discount=0.0)
            phase = add_phase(db, cfg=cfg)
            add_snapshot(db, [listing(server_id=5)])
            # ~3.7% discount: below the slot threshold, over the probe's
            state = self._state(db, phase, cfg, floor=2.35)
            slot_c = experiment._candidates(state, cfg, set(), [])[0]
            self.assertEqual(slot_c["mode"], "on-demand")
            probe_c = experiment._candidates(
                state, cfg, set(), [],
                spot_min_discount=cfg["spot_probe_min_discount"])[0]
            self.assertEqual(probe_c["mode"], "spot")
        finally:
            db.close()

    def test_rent_action_carries_spot_fields(self):
        db = make_db()
        try:
            cfg = self._spot_cfg()
            phase = add_phase(db, cfg=cfg)
            add_history(db, 5)
            add_snapshot(db, [listing(server_id=5)])
            self._state(db, phase, cfg)
            acts, _ = actions(db, phase, cfg=cfg)
            rents = [a for a in acts if a["type"] == "rent"]
            self.assertTrue(rents)
            self.assertEqual(rents[0]["mode"], "spot")
            self.assertEqual(rents[0]["spot_bid_usd_day"], 1.8)
        finally:
            db.close()


class CreationFeeForecastTests(unittest.TestCase):
    def test_predicted_return_uses_given_fee(self):
        od = experiment.predicted_return(1.0, 0.5, 4)
        spot_ret = experiment.predicted_return(
            1.0, 0.5, 4, Decimal("0.005"))
        self.assertAlmostEqual(od, 1.0 / (0.5 + 0.10 / 4))
        self.assertAlmostEqual(spot_ret, 1.0 / (0.5 + 0.005 / 4))

    def test_forecast_profit_uses_given_fee(self):
        self.assertAlmostEqual(
            experiment.forecast_profit(1.0, 0.5, 4),
            1.0 - (0.5 + 0.10 / 4))
        self.assertAlmostEqual(
            experiment.forecast_profit(
                1.0, 0.5, 4, Decimal("0.005")),
            1.0 - (0.5 + 0.005 / 4))
        # no hours -> incumbent math unchanged (fee sunk)
        self.assertAlmostEqual(
            experiment.forecast_profit(1.0, 0.5), 0.5)


class SpotRentExecTests(unittest.TestCase):
    def test_spot_recheck_fallback_to_on_demand(self):
        """A spot pick whose fresh quote no longer clears the
        threshold rents on-demand, emitting spot_fallback."""
        db = make_db()
        try:
            cfg = dict(CFG, spot_enabled=True)
            phase = add_phase(db, cfg=cfg)
            add_snapshot(db, [listing(server_id=5)])
            ctx = {"quote": {"cost_bound_usd": "0.30",
                             "hourly_cost_usd": "0.10",
                             "creation_fee_usd": "0.10"}}
            action = {"type": "rent", "server_id": 5, "reason": "fill",
                      "tier": "other", "mode": "spot",
                      "spot_bid_usd_day": 1.8}
            with mock.patch.object(
                    experiment.rent, "load_secrets",
                    return_value=("KEY", None)), \
                    mock.patch.object(
                        experiment.orders, "fetch_wallets",
                        return_value={"code": 0, "wallets": [
                            {"name": "USD-Blockchain",
                             "balance": 999}]}), \
                    mock.patch.object(
                        experiment.spot, "fetch_quote",
                        return_value={"code": 0, "exists": True,
                                      "market": {
                                          "server": {
                                              "min_pricing": {
                                                  "USD-Blockchain":
                                                  2.4},
                                              "online": True,
                                              "visibility":
                                              "public"},
                                          "offers": []}}) as fetch, \
                    mock.patch.object(
                        experiment.time, "sleep"), \
                    mock.patch.object(
                        experiment.rent, "quote_order",
                        return_value=ctx) as quote_mock, \
                    mock.patch.object(
                        experiment.rent, "create_order_flow",
                        return_value=(0, {"order_id": 42})):
                experiment.execute(
                    db, ":memory:", "/tmp/env", phase, cfg,
                    [action], NOW, live=True, say=lambda *_a: None)
            fetch.assert_called_once()
            self.assertEqual(quote_mock.call_args.kwargs["mode"],
                             "on-demand")
            row = db.execute(
                "SELECT mode FROM experiment_rentals"
                " WHERE order_id=42").fetchone()
            self.assertEqual(row["mode"], "on-demand")
            ev = db.execute(
                "SELECT detail_json FROM experiment_events"
                " WHERE kind='rent'").fetchone()
            detail = json.loads(ev["detail_json"])
            self.assertEqual(detail["mode"], "on-demand")
            self.assertTrue(detail["spot_fallback"])
        finally:
            db.close()

    def test_spot_recheck_fetch_failure_retries_on_demand(self):
        """The POST-stage recheck aborts on a dead quote fetch the
        same as on a market move; only the latter should kill the
        pick -- a fetch blip retries on-demand."""
        db = make_db()
        try:
            cfg = dict(CFG, spot_enabled=True)
            phase = add_phase(db, cfg=cfg)
            add_snapshot(db, [listing(server_id=5), listing(server_id=6)])
            ctx = {"quote": {"cost_bound_usd": "0.30",
                             "hourly_cost_usd": "0.10",
                             "creation_fee_usd": "0.10"}}
            action = {"type": "rent", "server_id": 5, "reason": "fill",
                      "tier": "other", "mode": "spot",
                      "spot_bid_usd_day": 1.8}
            with mock.patch.object(
                    experiment.rent, "load_secrets",
                    return_value=("KEY", None)), \
                    mock.patch.object(
                        experiment.orders, "fetch_wallets",
                        return_value={"code": 0, "wallets": [
                            {"name": "USD-Blockchain",
                             "balance": 999}]}), \
                    mock.patch.object(
                        experiment.spot, "fetch_quote",
                        return_value=json.loads(
                            quote_record(floor=1.8,
                                         minutes_ago=0)["raw_json"])), \
                    mock.patch.object(
                        experiment.time, "sleep"), \
                    mock.patch.object(
                        experiment.rent, "quote_order",
                        return_value=ctx) as quote_mock, \
                    mock.patch.object(
                        experiment.rent, "create_order_flow",
                        side_effect=[
                            (2, {"stage": "spot_recheck",
                                 "spot_fetch_failed": True}),
                            (0, {"order_id": 42})]) as create:
                experiment.execute(
                    db, ":memory:", "/tmp/env", phase, cfg,
                    [action], NOW, live=True, say=lambda *_a: None)
            modes = [c.kwargs["mode"]
                     for c in quote_mock.call_args_list]
            self.assertEqual(modes, ["spot", "on-demand"])
            self.assertEqual(create.call_count, 2)
            row = db.execute(
                "SELECT mode FROM experiment_rentals"
                " WHERE order_id=42").fetchone()
            self.assertEqual(row["mode"], "on-demand")
            detail = json.loads(db.execute(
                "SELECT detail_json FROM experiment_events"
                " WHERE kind='rent'").fetchone()["detail_json"])
            self.assertEqual(detail["mode"], "on-demand")
            self.assertTrue(detail["spot_fallback"])
            # a market-move abort (no fetch flag) must NOT retry
            create.side_effect = [
                (2, {"stage": "spot_recheck",
                     "spot_fetch_failed": False})]
            create.reset_mock()
            quote_mock.reset_mock()
            with mock.patch.object(
                    experiment.rent, "load_secrets",
                    return_value=("KEY", None)), \
                    mock.patch.object(
                        experiment.orders, "fetch_wallets",
                        return_value={"code": 0, "wallets": [
                            {"name": "USD-Blockchain",
                             "balance": 999}]}), \
                    mock.patch.object(
                        experiment.spot, "fetch_quote",
                        return_value=json.loads(
                            quote_record(floor=1.8,
                                         minutes_ago=0)["raw_json"])), \
                    mock.patch.object(
                        experiment.time, "sleep"), \
                    mock.patch.object(
                        experiment.rent, "quote_order",
                        return_value=ctx) as quote_mock2, \
                    mock.patch.object(
                        experiment.rent, "create_order_flow",
                        side_effect=[
                            (2, {"stage": "spot_recheck",
                                 "spot_fetch_failed": False})]) \
                    as create2:
                experiment.execute(
                    db, ":memory:", "/tmp/env", phase, cfg,
                    [dict(action, server_id=6)], NOW, live=True,
                    say=lambda *_a: None)
            create2.assert_called_once()
            self.assertEqual(len(quote_mock2.call_args_list), 1)
            self.assertEqual(
                quote_mock2.call_args.kwargs["mode"], "spot")
        finally:
            db.close()

    def test_spot_rent_passes_bid_and_floor(self):
        db = make_db()
        try:
            cfg = dict(CFG, spot_enabled=True)
            phase = add_phase(db, cfg=cfg)
            add_snapshot(db, [listing(server_id=5)])
            ctx = {"quote": {"cost_bound_usd": "0.30",
                             "hourly_cost_usd": "0.0759",
                             "creation_fee_usd": "0.005",
                             "mode": "spot",
                             "spot_bid_usd_day": "1.8"}}
            action = {"type": "rent", "server_id": 5, "reason": "fill",
                      "tier": "other", "mode": "spot",
                      "spot_bid_usd_day": 1.8}
            with mock.patch.object(
                    experiment.rent, "load_secrets",
                    return_value=("KEY", None)), \
                    mock.patch.object(
                        experiment.orders, "fetch_wallets",
                        return_value={"code": 0, "wallets": [
                            {"name": "USD-Blockchain",
                             "balance": 999}]}), \
                    mock.patch.object(
                        experiment.spot, "fetch_quote",
                        return_value=json.loads(
                            quote_record(floor=1.8,
                                         minutes_ago=0)["raw_json"])
                        ) as fetch, \
                    mock.patch.object(
                        experiment.time, "sleep"), \
                    mock.patch.object(
                        experiment.rent, "quote_order",
                        return_value=ctx) as quote_mock, \
                    mock.patch.object(
                        experiment.rent, "create_order_flow",
                        return_value=(0, {"order_id": 42})):
                experiment.execute(
                    db, ":memory:", "/tmp/env", phase, cfg,
                    [action], NOW, live=True, say=lambda *_a: None)
            fetch.assert_called_once()
            kw = quote_mock.call_args.kwargs
            self.assertEqual(kw["mode"], "spot")
            self.assertEqual(kw["spot_bid_usd_day"], 1.8)
            self.assertEqual(kw["spot_floor_usd_day"], 1.8)
            row = db.execute(
                "SELECT mode FROM experiment_rentals"
                " WHERE order_id=42").fetchone()
            self.assertEqual(row["mode"], "spot")
            detail = json.loads(db.execute(
                "SELECT detail_json FROM experiment_events"
                " WHERE kind='rent'").fetchone()["detail_json"])
            self.assertEqual(detail["mode"], "spot")
            self.assertEqual(detail["spot_bid_usd_day"], 1.8)
            self.assertAlmostEqual(detail["spot_discount"],
                                   1 - (1.8 / 24 * 1.0125) / 0.105,
                                   places=6)
        finally:
            db.close()

    def test_on_demand_action_discovers_spot(self):
        """decide() picked on-demand (no cached quote for a mid-ranked
        server) but the fresh rent-time read clears the probe
        threshold: the rent goes spot, carries the spot fields and is
        marked discovered -- never a fallback."""
        db = make_db()
        try:
            cfg = dict(CFG, spot_enabled=True)
            phase = add_phase(db, cfg=cfg)
            add_snapshot(db, [listing(server_id=5)])
            ctx = {"quote": {"cost_bound_usd": "0.30",
                             "hourly_cost_usd": "0.0992",
                             "creation_fee_usd": "0.005",
                             "mode": "spot",
                             "spot_bid_usd_day": "2.35"}}
            # floor 2.35/day -> 0.0991/hr spot vs 0.105 on-demand:
            # ~5.6% off, over the probe's 0% bar, under the slot's 10%
            action = {"type": "rent", "server_id": 5, "reason": "probe",
                      "tier": "other", "role": "probe", "burst_id": 1,
                      "mode": "on-demand"}
            with mock.patch.object(
                    experiment.rent, "load_secrets",
                    return_value=("KEY", None)), \
                    mock.patch.object(
                        experiment.orders, "fetch_wallets",
                        return_value={"code": 0, "wallets": [
                            {"name": "USD-Blockchain",
                             "balance": 999}]}), \
                    mock.patch.object(
                        experiment.spot, "fetch_quote",
                        return_value=json.loads(
                            quote_record(floor=2.35,
                                         minutes_ago=0)["raw_json"])
                        ) as fetch, \
                    mock.patch.object(
                        experiment.time, "sleep"), \
                    mock.patch.object(
                        experiment.rent, "quote_order",
                        return_value=ctx) as quote_mock, \
                    mock.patch.object(
                        experiment.rent, "create_order_flow",
                        return_value=(0, {"order_id": 42})):
                experiment.execute(
                    db, ":memory:", "/tmp/env", phase, cfg,
                    [action], NOW, live=True, say=lambda *_a: None)
            fetch.assert_called_once()
            kw = quote_mock.call_args.kwargs
            self.assertEqual(kw["mode"], "spot")
            self.assertEqual(kw["spot_bid_usd_day"], 2.35)
            self.assertEqual(kw["spot_floor_usd_day"], 2.35)
            row = db.execute(
                "SELECT mode FROM experiment_rentals"
                " WHERE order_id=42").fetchone()
            self.assertEqual(row["mode"], "spot")
            detail = json.loads(db.execute(
                "SELECT detail_json FROM experiment_events"
                " WHERE kind='rent'").fetchone()["detail_json"])
            self.assertEqual(detail["mode"], "spot")
            self.assertEqual(detail["spot_bid_usd_day"], 2.35)
            self.assertEqual(detail["spot_floor_usd_day"], 2.35)
            self.assertAlmostEqual(detail["spot_discount"],
                                   1 - (2.35 / 24 * 1.0125) / 0.105,
                                   places=6)
            self.assertTrue(detail["spot_discovered"])
            self.assertNotIn("spot_fallback", detail)
        finally:
            db.close()

    def test_on_demand_action_stays_when_quote_not_clearing(self):
        """A fresh quote that misses the threshold leaves the
        on-demand pick on-demand; no spot fields reach the event."""
        db = make_db()
        try:
            cfg = dict(CFG, spot_enabled=True)
            phase = add_phase(db, cfg=cfg)
            add_snapshot(db, [listing(server_id=5)])
            ctx = {"quote": {"cost_bound_usd": "0.30",
                             "hourly_cost_usd": "0.10",
                             "creation_fee_usd": "0.10"}}
            action = {"type": "rent", "server_id": 5, "reason": "fill",
                      "tier": "other", "mode": "on-demand"}
            with mock.patch.object(
                    experiment.rent, "load_secrets",
                    return_value=("KEY", None)), \
                    mock.patch.object(
                        experiment.orders, "fetch_wallets",
                        return_value={"code": 0, "wallets": [
                            {"name": "USD-Blockchain",
                             "balance": 999}]}), \
                    mock.patch.object(
                        experiment.spot, "fetch_quote",
                        return_value=json.loads(
                            quote_record(floor=2.4,
                                         minutes_ago=0)["raw_json"])
                        ) as fetch, \
                    mock.patch.object(
                        experiment.time, "sleep"), \
                    mock.patch.object(
                        experiment.rent, "quote_order",
                        return_value=ctx) as quote_mock, \
                    mock.patch.object(
                        experiment.rent, "create_order_flow",
                        return_value=(0, {"order_id": 42})):
                experiment.execute(
                    db, ":memory:", "/tmp/env", phase, cfg,
                    [action], NOW, live=True, say=lambda *_a: None)
            fetch.assert_called_once()
            kw = quote_mock.call_args.kwargs
            self.assertEqual(kw["mode"], "on-demand")
            self.assertIsNone(kw["spot_bid_usd_day"])
            self.assertIsNone(kw["spot_floor_usd_day"])
            row = db.execute(
                "SELECT mode FROM experiment_rentals"
                " WHERE order_id=42").fetchone()
            self.assertEqual(row["mode"], "on-demand")
            detail = json.loads(db.execute(
                "SELECT detail_json FROM experiment_events"
                " WHERE kind='rent'").fetchone()["detail_json"])
            self.assertEqual(detail["mode"], "on-demand")
            for key in ("spot_bid_usd_day", "spot_floor_usd_day",
                        "spot_discount", "spot_fallback",
                        "spot_discovered"):
                self.assertNotIn(key, detail)
        finally:
            db.close()

    def test_outbid_cooled_server_skips_fetch(self):
        """A recent outbid cools the server: the fresh check is skipped
        entirely (no rate-limited request) and the spot pick degrades
        to on-demand with spot_fallback."""
        db = make_db()
        try:
            cfg = dict(CFG, spot_enabled=True)
            phase = add_phase(db, cfg=cfg)
            add_snapshot(db, [listing(server_id=5)])
            add_fleet(db, 1, 5)
            add_rental(db, phase["phase_id"], 1, 5, 40)
            db.execute(
                "UPDATE experiment_rentals SET ended_at=?,"
                " end_reason='outbid' WHERE order_id=1", (iso(10),))
            db.commit()
            ctx = {"quote": {"cost_bound_usd": "0.30",
                             "hourly_cost_usd": "0.10",
                             "creation_fee_usd": "0.10"}}
            action = {"type": "rent", "server_id": 5, "reason": "fill",
                      "tier": "other", "mode": "spot",
                      "spot_bid_usd_day": 1.8}
            with mock.patch.object(
                    experiment.rent, "load_secrets",
                    return_value=("KEY", None)), \
                    mock.patch.object(
                        experiment.orders, "fetch_wallets",
                        return_value={"code": 0, "wallets": [
                            {"name": "USD-Blockchain",
                             "balance": 999}]}), \
                    mock.patch.object(
                        experiment.spot, "fetch_quote") as fetch, \
                    mock.patch.object(
                        experiment.time, "sleep"), \
                    mock.patch.object(
                        experiment.rent, "quote_order",
                        return_value=ctx) as quote_mock, \
                    mock.patch.object(
                        experiment.rent, "create_order_flow",
                        return_value=(0, {"order_id": 42})):
                experiment.execute(
                    db, ":memory:", "/tmp/env", phase, cfg,
                    [action], NOW, live=True, say=lambda *_a: None)
            fetch.assert_not_called()
            self.assertEqual(quote_mock.call_args.kwargs["mode"],
                             "on-demand")
            detail = json.loads(db.execute(
                "SELECT detail_json FROM experiment_events"
                " WHERE kind='rent'").fetchone()["detail_json"])
            self.assertEqual(detail["mode"], "on-demand")
            self.assertTrue(detail["spot_fallback"])
        finally:
            db.close()

    def test_spot_disabled_never_fetches(self):
        """spot_enabled=False keeps the byte-identical pre-spot path:
        no quote fetch, no sleep, on-demand quote_order call even when
        the action claims mode=spot."""
        db = make_db()
        try:
            phase = add_phase(db, cfg=CFG)
            add_snapshot(db, [listing(server_id=5)])
            ctx = {"quote": {"cost_bound_usd": "0.30",
                             "hourly_cost_usd": "0.10",
                             "creation_fee_usd": "0.10"}}
            action = {"type": "rent", "server_id": 5, "reason": "fill",
                      "tier": "other", "mode": "spot",
                      "spot_bid_usd_day": 1.8}
            with mock.patch.object(
                    experiment.rent, "load_secrets",
                    return_value=("KEY", None)), \
                    mock.patch.object(
                        experiment.orders, "fetch_wallets",
                        return_value={"code": 0, "wallets": [
                            {"name": "USD-Blockchain",
                             "balance": 999}]}), \
                    mock.patch.object(
                        experiment.spot, "fetch_quote") as fetch, \
                    mock.patch.object(
                        experiment.time, "sleep") as sleep, \
                    mock.patch.object(
                        experiment.rent, "quote_order",
                        return_value=ctx) as quote_mock, \
                    mock.patch.object(
                        experiment.rent, "create_order_flow",
                        return_value=(0, {"order_id": 42})):
                experiment.execute(
                    db, ":memory:", "/tmp/env", phase, CFG,
                    [action], NOW, live=True, say=lambda *_a: None)
            fetch.assert_not_called()
            sleep.assert_not_called()
            self.assertEqual(quote_mock.call_args.kwargs["mode"],
                             "on-demand")
            detail = json.loads(db.execute(
                "SELECT detail_json FROM experiment_events"
                " WHERE kind='rent'").fetchone()["detail_json"])
            self.assertEqual(detail["mode"], "on-demand")
            for key in ("spot_bid_usd_day", "spot_floor_usd_day",
                        "spot_discount", "spot_fallback",
                        "spot_discovered"):
                self.assertNotIn(key, detail)
        finally:
            db.close()


class PreMigrationDbTests(unittest.TestCase):
    """experiment_rentals without the `mode` column (pre-migration
    schema) must still read on read-only paths; the ALTER only runs
    on the write path."""

    def _old_db(self):
        import market, blacklist, fleet_store, pool_stats
        db = market.connect_db(":memory:")
        fleet_store.initialize(db)
        blacklist.initialize(db)
        pool_stats.initialize(db)
        db.executescript(experiment.SCHEMA)
        return db

    def test_build_state_and_summary_without_mode_column(self):
        db = self._old_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [listing(server_id=5)])
            add_fleet(db, 1, 5)
            add_rental(db, phase["phase_id"], 1, 5, 30)
            assert "mode" not in {
                r[1] for r in db.execute(
                    "PRAGMA table_info(experiment_rentals)")}
            state = experiment.build_state(
                db, phase, NOW, CFG, apply_endings=False,
                say=lambda *_a: None)
            self.assertEqual(state["rentals"][0]["mode"], "on-demand")
            summary = experiment.summary(db, NOW)
            self.assertEqual(
                summary["rentals"][0]["server_id"], 5)
            self.assertEqual(state["spot_quotes"], {})
        finally:
            db.close()


def add_inventory_items(db, items, minutes_ago=0):
    """A fresh good my_orders snapshot carrying full item dicts."""
    db.executescript(orders.SCHEMA)
    db.execute(
        "INSERT INTO order_inventory(captured_at, ok, orders_json)"
        " VALUES (?,?,?)",
        (iso(minutes_ago), 1, json.dumps(items)))
    db.commit()


def spot_rental(db, phase, order_id, server_id, created_min=30,
                role="slot", burst_id=None, hourly="0.10"):
    add_fleet(db, order_id, server_id)
    add_rental(db, phase["phase_id"], order_id, server_id, created_min,
               role=role, burst_id=burst_id, hourly=hourly)
    db.execute("UPDATE experiment_rentals SET mode='spot'"
               " WHERE order_id=?", (order_id,))
    db.commit()


class OrderPausedTests(unittest.TestCase):
    def test_encodings(self):
        self.assertTrue(orders.order_paused({"status": "paused"}))
        self.assertTrue(orders.order_paused({"expired": True}))
        self.assertFalse(orders.order_paused({"status": "running"}))
        self.assertFalse(orders.order_paused({"status": "pending"}))
        self.assertFalse(orders.order_paused({}))
        self.assertFalse(orders.order_paused({"expired": False}))
        self.assertFalse(orders.order_paused({"status": 7}))
        self.assertFalse(orders.order_paused({"status": ["paused"]}))
        self.assertFalse(orders.order_paused(None))
        self.assertFalse(orders.order_paused("paused"))

    def test_sanitize_carries_status_and_expired(self):
        items = orders.sanitize_orders({"code": 0, "orders": [
            {"id": 1, "status": "paused", "expired": True},
            {"id": 2, "status": 7, "expired": "yes"},
            {"id": 3}]})
        self.assertEqual(
            [(i["status"], i["expired"]) for i in items],
            [("paused", True), (None, None), (None, None)])

    def test_paused_order_ids_matches_fresh_order_ids_contract(self):
        inv = {"ok": True, "captured_at": NOW.isoformat(),
               "orders": [{"order_id": 1, "status": "paused"},
                          {"order_id": 2, "status": "running"},
                          {"order_id": 3}]}
        self.assertEqual(orders.paused_order_ids(inv, NOW), {1})
        self.assertEqual(orders.fresh_order_ids(inv, NOW), {1, 2, 3})
        stale = dict(inv, captured_at=iso(20))
        self.assertIsNone(orders.paused_order_ids(stale, NOW))
        self.assertIsNone(orders.fresh_order_ids(stale, NOW))
        self.assertIsNone(orders.paused_order_ids({"ok": False}, NOW))
        self.assertIsNone(orders.paused_order_ids(
            {"ok": True, "captured_at": "garbage"}, NOW))


class PausedRentalTests(unittest.TestCase):
    def test_paused_spot_ends_outbid_no_cancel_no_blame(self):
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [listing(server_id=5)])
            spot_rental(db, phase, 1, 5)
            add_inventory_items(db, [{"order_id": 1, "server_id": 5,
                                      "spot": True,
                                      "status": "paused"}])
            with mock.patch.object(
                    experiment.rent, "cancel_managed_order",
                    side_effect=AssertionError("cancel POST issued")):
                acts, state = actions(db, phase)
            row = db.execute(
                "SELECT ended_at, end_reason FROM experiment_rentals"
                " WHERE order_id=1").fetchone()
            self.assertIsNotNone(row["ended_at"])
            self.assertEqual(row["end_reason"], "outbid")
            self.assertEqual(state["rentals"], [])
            self.assertEqual(kinds(acts, "cancel"), [])
            self.assertFalse(blacklist.active(db, NOW))
            ev = json.loads(db.execute(
                "SELECT detail_json FROM experiment_events"
                " WHERE kind='ended' AND order_id=1"
                ).fetchone()["detail_json"])
            self.assertEqual(ev["reason"], "outbid")
        finally:
            db.close()

    def test_paused_ondemand_keeps_row_no_unreachable_cancel(self):
        """A paused on-demand rig is neither ended nor declared
        unreachable: it stays listed, stops being SSH-polled, and
        surfaces only as a throttled event."""
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [listing(server_id=5)])
            add_fleet(db, 1, 5)
            add_rental(db, phase["phase_id"], 1, 5, 40)
            for t in (14, 10, 5, 1):
                add_obs(db, 1, t, None)
            add_inventory_items(
                db, [{"order_id": 1, "server_id": 5,
                      "status": "paused"}])
            acts, state = actions(db, phase)
            self.assertTrue(state["rentals"][0]["paused"])
            self.assertEqual(kinds(acts, "cancel"), [])
            self.assertFalse(blacklist.active(db, NOW))
            self.assertTrue(db.execute(
                "SELECT 1 FROM experiment_events WHERE kind='paused'"
                " AND order_id=1").fetchone())
        finally:
            db.close()

    def test_exec_cancel_deblames_paused_rig(self):
        """Belt and braces: even an unreachable cancel executed while
        the order reads paused ends as 'paused', not 'unreachable',
        and never reaches the blacklist."""
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [listing(server_id=5)])
            add_fleet(db, 1, 5)
            add_rental(db, phase["phase_id"], 1, 5, 40)
            add_inventory_items(
                db, [{"order_id": 1, "server_id": 5,
                      "status": "paused"}])
            action = {"type": "cancel", "order_id": 1, "server_id": 5,
                      "tier": "other", "reason": "unreachable",
                      "rerent_at": None}
            with mock.patch.object(
                    experiment.rent, "cancel_managed_order",
                    return_value=(0, {})):
                experiment.execute(
                    db, ":memory:", "/tmp/env", phase, CFG, [action],
                    NOW, live=True, say=lambda *_a: None)
            row = db.execute(
                "SELECT end_reason FROM experiment_rentals"
                " WHERE order_id=1").fetchone()
            self.assertEqual(row["end_reason"], "paused")
            self.assertFalse(blacklist.active(db, NOW))
        finally:
            db.close()

    def test_telemetry_skips_ssh_for_paused_order(self):
        import market, tempfile
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "db.sqlite3")
            db = experiment.open_db(db_path)
            add_fleet(db, 1, 5)
            db.executescript(orders.SCHEMA)
            # collect_once uses the real clock for inventory freshness
            db.execute(
                "INSERT INTO order_inventory(captured_at, ok,"
                " orders_json) VALUES (?,?,?)",
                (market.utcnow(), 1, json.dumps(
                    [{"order_id": 1, "server_id": 5,
                      "status": "paused"}])))
            db.commit()
            db.close()
            with mock.patch.object(
                    telemetry.rig_ssh, "ssh_json",
                    side_effect=AssertionError("SSH polled")):
                telemetry.collect_once(db_path)
            db = experiment.open_db(db_path)
            try:
                self.assertFalse(db.execute(
                    "SELECT 1 FROM rig_observations"
                    " WHERE order_id=1").fetchone())
            finally:
                db.close()


class ReactivationTests(unittest.TestCase):
    def _ended_outbid(self, db, phase, order_id=1, server_id=5,
                      ended_min=180, created_min=400):
        spot_rental(db, phase, order_id, server_id, created_min)
        db.execute(
            "UPDATE experiment_rentals SET ended_at=?, end_reason='outbid'"
            " WHERE order_id=?", (iso(ended_min), order_id))
        db.commit()

    def _resumed_inventory(self, db, order_id=1, server_id=5):
        add_inventory_items(
            db, [{"order_id": order_id, "server_id": server_id,
                  "spot": True, "status": "running"}])

    def test_resumed_spot_reopens_as_probe(self):
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [listing(server_id=5)])
            self._ended_outbid(db, phase)
            self._resumed_inventory(db)
            _, state = actions(db, phase)
            row = db.execute(
                "SELECT ended_at, end_reason, role, burst_id,"
                " resumed_at FROM experiment_rentals"
                " WHERE order_id=1").fetchone()
            self.assertIsNone(row["ended_at"])
            self.assertEqual(row["role"], "probe")
            self.assertIsNotNone(row["resumed_at"])
            burst = db.execute(
                "SELECT * FROM experiment_bursts"
                " WHERE burst_id=?", (row["burst_id"],)).fetchone()
            self.assertEqual(burst["trigger"], "reactivated")
            self.assertIsNone(burst["trigger_order_id"])
            probe = state["rentals"][0]
            self.assertEqual((probe["role"], probe["mode"]),
                             ("probe", "spot"))
            ev = db.execute(
                "SELECT detail_json FROM experiment_events"
                " WHERE kind='reactivated'").fetchone()
            detail = json.loads(ev["detail_json"])
            self.assertEqual(detail["burst_id"], row["burst_id"])
            self.assertAlmostEqual(detail["paused_min"], 180.0, places=1)
            self.assertTrue(db.execute(
                "SELECT 1 FROM experiment_events"
                " WHERE kind='burst_started'").fetchone())
        finally:
            db.close()

    def test_resumed_rebaselines_clocks(self):
        """Hours spent paused must not read as a dry spell: all
        observation-derived state restarts at resumed_at."""
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [listing(server_id=5)])
            self._ended_outbid(db, phase)
            # pre-pause evidence that would trip unreachable + dry
            # rules if the resume didn't re-baseline
            for t in (380, 370, 360):
                add_obs(db, 1, t, status(submitted=3))
            for t in (350, 340):
                add_obs(db, 1, t, None)
            self._resumed_inventory(db)
            acts, state = actions(db, phase)
            probe = state["rentals"][0]
            self.assertIsNone(probe["t0"])
            self.assertEqual(probe["share_arrivals"], [])
            self.assertIsNone(probe["quiet_since"])
            self.assertFalse(probe["recent_errors_only"])
            self.assertLess(probe["age"].total_seconds(), 60)
            self.assertEqual(kinds(acts, "cancel"), [])
        finally:
            db.close()

    def test_full_fleet_probes_against_weakest_slot(self):
        db = make_db()
        try:
            cfg = dict(CFG, max_rentals=1)
            phase = add_phase(db, cfg=cfg)
            add_snapshot(db, [listing(server_id=5),
                              listing(server_id=6)])
            add_fleet(db, 2, 6)
            add_rental(db, phase["phase_id"], 2, 6, 200)
            self._ended_outbid(db, phase)
            self._resumed_inventory(db)
            _, state = actions(db, phase, cfg=cfg)
            row = db.execute(
                "SELECT burst_id FROM experiment_rentals"
                " WHERE order_id=1").fetchone()
            burst = db.execute(
                "SELECT * FROM experiment_bursts"
                " WHERE burst_id=?", (row["burst_id"],)).fetchone()
            self.assertEqual(burst["trigger_order_id"], 2)
        finally:
            db.close()

    def test_unjudged_burst_blocks_reactivation(self):
        """A resume while a burst is pending can never queue: the
        standing order is cancelled instead."""
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [listing(server_id=5)])
            self._ended_outbid(db, phase)
            add_burst(db, phase["phase_id"], 2, trigger="dry_retry")
            self._resumed_inventory(db)
            acts, state = actions(db, phase)
            row = db.execute(
                "SELECT ended_at, end_reason, role"
                " FROM experiment_rentals WHERE order_id=1").fetchone()
            self.assertIsNotNone(row["ended_at"])
            self.assertEqual(row["role"], "slot")
            cancels = kinds(acts, "cancel")
            self.assertEqual(len(cancels), 1)
            self.assertEqual(cancels[0]["reason"], "reactivate_blocked")
            with mock.patch.object(
                    experiment.rent, "cancel_managed_order",
                    return_value=(0, {})) as cancel:
                experiment.execute(
                    db, ":memory:", "/tmp/env", phase, CFG,
                    cancels, NOW, live=True, say=lambda *_a: None)
            cancel.assert_called_once()
            self.assertEqual(cancel.call_args.args[0], 1)
        finally:
            db.close()

    def test_still_paused_stays_ended(self):
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [listing(server_id=5)])
            self._ended_outbid(db, phase)
            add_inventory_items(
                db, [{"order_id": 1, "server_id": 5, "spot": True,
                      "status": "paused"}])
            acts, _ = actions(db, phase)
            row = db.execute(
                "SELECT role, end_reason FROM experiment_rentals"
                " WHERE order_id=1").fetchone()
            self.assertEqual((row["role"], row["end_reason"]),
                             ("slot", "outbid"))
            self.assertEqual(kinds(acts, "cancel"), [])
        finally:
            db.close()


class ReactivationJudgeTests(unittest.TestCase):
    """A reactivated probe that lands on a full fleet auditions
    against the weakest slot under upgrade semantics: it takes the
    seat only by beating the incumbent's measured score by
    upgrade_margin. Below margin the incumbent is kept -- never
    dry-cancelled for a foreign probe's failure."""

    def _full_fleet(self, db, phase, hourly="0.10"):
        # the weakest slot: v(90)/$0.10 = 94.9; quiet since long
        # before the burst so `recovered` stays False
        spot_rental(db, phase, 1, 11, 60, hourly=hourly)
        add_obs(db, 1, 40, status(submitted=5))
        add_obs(db, 1, 0.5, status(submitted=5))

    def _probe(self, db, phase, burst_id, hourly="0.10"):
        # spot probe aged 20.5min: past spot_probe_judge_min(20) so
        # the judge may fire, still inside the 21min cancel window
        spot_rental(db, phase, 10, 20, 20.5, role="probe",
                    burst_id=burst_id, hourly=hourly)

    def test_full_fleet_beats_margin_upgrades_slot(self):
        # v(170)/($0.05+$0.10/6) = 195.6 > 94.9 * 1.25 = 118.6: the
        # probe takes the seat, the incumbent is 'upgraded' out
        db = make_db()
        try:
            cfg = dict(CFG, max_rentals=1)
            phase = add_phase(db, cfg=cfg)
            add_snapshot(db, [])
            self._full_fleet(db, phase)
            burst_id = add_burst(db, phase["phase_id"], 6,
                                 trigger="reactivated",
                                 trigger_order_id=1, n_target=1)
            self._probe(db, phase, burst_id, hourly="0.05")
            add_obs(db, 10, 21, status(submitted=0, cpu=50, gpu=120))
            add_obs(db, 10, 1, status(submitted=5, cpu=50, gpu=120))
            acts, _ = actions(db, phase, cfg=cfg)
            judge = kinds(acts, "judge")[0]
            self.assertEqual(judge["verdict"], "upgraded")
            self.assertEqual(judge["promote"], 10)
            self.assertEqual(judge["cancel_original"]["order_id"], 1)
            self.assertEqual(judge["cancel_original"]["reason"],
                             "upgraded")
            self.assertEqual(judge["cancel"], [])
            stats = judge["stats"]
            self.assertEqual(stats["orig_its"], 90)
            self.assertEqual(stats["probe_its"], 170)
        finally:
            db.close()

    def test_full_fleet_below_margin_keeps_original(self):
        # a producing probe that misses the margin must NOT displace
        # the incumbent: v(15)/($0.02+fee/6) = 105.6 < 118.6
        db = make_db()
        try:
            cfg = dict(CFG, max_rentals=1)
            phase = add_phase(db, cfg=cfg)
            add_snapshot(db, [])
            self._full_fleet(db, phase)
            burst_id = add_burst(db, phase["phase_id"], 6,
                                 trigger="reactivated",
                                 trigger_order_id=1, n_target=1)
            self._probe(db, phase, burst_id, hourly="0.02")
            add_obs(db, 10, 21, status(submitted=0, cpu=5, gpu=10))
            add_obs(db, 10, 1, status(submitted=5, cpu=5, gpu=10))
            acts, _ = actions(db, phase, cfg=cfg)
            judge = kinds(acts, "judge")[0]
            self.assertEqual(judge["verdict"], "kept_original")
            self.assertIsNone(judge["promote"])
            self.assertIsNone(judge["cancel_original"])
            self.assertIsNone(judge["retry"])
            self.assertEqual(
                [c["order_id"] for c in judge["cancel"]], [10])
            self.assertEqual(judge["cancel"][0]["reason"],
                             "probe_lost")
        finally:
            db.close()

    def test_full_fleet_no_finders_keeps_original(self):
        # the probe produced nothing: the incumbent is not on trial,
        # so no dry cancel and no retry arm -- the generic
        # original-is-not-None branch would have dry-cancelled it
        db = make_db()
        try:
            cfg = dict(CFG, max_rentals=1)
            phase = add_phase(db, cfg=cfg)
            add_snapshot(db, [])
            self._full_fleet(db, phase)
            burst_id = add_burst(db, phase["phase_id"], 6,
                                 trigger="reactivated",
                                 trigger_order_id=1, n_target=1)
            self._probe(db, phase, burst_id)
            add_obs(db, 10, 21, status(submitted=0))
            add_obs(db, 10, 1, status(submitted=0))
            acts, _ = actions(db, phase, cfg=cfg)
            judge = kinds(acts, "judge")[0]
            self.assertEqual(judge["verdict"], "kept_original")
            self.assertIsNone(judge["promote"])
            self.assertIsNone(judge["cancel_original"])
            self.assertIsNone(judge["retry"])
            self.assertEqual(
                [c["order_id"] for c in judge["cancel"]], [10])
        finally:
            db.close()

    def test_vacancy_finder_promotes(self):
        # trigger_order_id=None (the fleet had a free seat): no
        # incumbent resolves, the vacancy path promotes a finder
        db = make_db()
        try:
            cfg = dict(CFG, max_rentals=2)
            phase = add_phase(db, cfg=cfg)
            add_snapshot(db, [])
            burst_id = add_burst(db, phase["phase_id"], 6,
                                 trigger="reactivated", n_target=1)
            self._probe(db, phase, burst_id)
            add_obs(db, 10, 21, status(submitted=0, cpu=50, gpu=120))
            add_obs(db, 10, 1, status(submitted=5, cpu=50, gpu=120))
            acts, _ = actions(db, phase, cfg=cfg)
            judge = kinds(acts, "judge")[0]
            self.assertEqual(judge["verdict"], "promoted")
            self.assertEqual(judge["promote"], 10)
            self.assertIsNone(judge["cancel_original"])
        finally:
            db.close()

    def test_vacancy_no_finders(self):
        # the freed seat stays a normal-fill vacancy: 'no_finders'
        # with no retry arm
        db = make_db()
        try:
            cfg = dict(CFG, max_rentals=2)
            phase = add_phase(db, cfg=cfg)
            add_snapshot(db, [])
            burst_id = add_burst(db, phase["phase_id"], 6,
                                 trigger="reactivated", n_target=1)
            self._probe(db, phase, burst_id)
            add_obs(db, 10, 21, status(submitted=0))
            add_obs(db, 10, 1, status(submitted=0))
            acts, _ = actions(db, phase, cfg=cfg)
            judge = kinds(acts, "judge")[0]
            self.assertEqual(judge["verdict"], "no_finders")
            self.assertIsNone(judge["promote"])
            self.assertIsNone(judge["cancel_original"])
            self.assertIsNone(judge["retry"])
            self.assertEqual(
                [c["order_id"] for c in judge["cancel"]], [10])
        finally:
            db.close()

    def test_incumbent_ended_mid_audition_promotes_into_seat(self):
        # the trigger slot vanished under the audition (host cancel):
        # original resolves None and a qualifying probe promotes into
        # the freed seat via the vacancy path
        db = make_db()
        try:
            cfg = dict(CFG, max_rentals=1)
            phase = add_phase(db, cfg=cfg)
            add_snapshot(db, [])
            self._full_fleet(db, phase)
            db.execute(
                "UPDATE experiment_rentals SET ended_at=?,"
                " end_reason='term_end' WHERE order_id=1", (iso(1),))
            db.commit()
            burst_id = add_burst(db, phase["phase_id"], 6,
                                 trigger="reactivated",
                                 trigger_order_id=1, n_target=1)
            self._probe(db, phase, burst_id, hourly="0.05")
            add_obs(db, 10, 21, status(submitted=0, cpu=50, gpu=120))
            add_obs(db, 10, 1, status(submitted=5, cpu=50, gpu=120))
            acts, _ = actions(db, phase, cfg=cfg)
            judge = kinds(acts, "judge")[0]
            self.assertEqual(judge["verdict"], "promoted")
            self.assertEqual(judge["promote"], 10)
            self.assertIsNone(judge["cancel_original"])
        finally:
            db.close()


class ResumedBaselineTests(unittest.TestCase):
    """share_arrivals on a reactivated order seeds the cumulative
    counter at the last good pre-resume reading, so a container that
    survived the outbid pause doesn't fabricate its whole pre-pause
    share count as a single t0 arrival."""

    def _reopened(self, db, phase, resumed_min=60):
        """An outbid row re-opened as a probe resumed `resumed_min`
        ago -- the state the reactivation path writes."""
        spot_rental(db, phase, 1, 5, 400)
        db.execute(
            "UPDATE experiment_rentals SET ended_at=NULL,"
            " end_reason=NULL, role='probe', resumed_at=?"
            " WHERE order_id=1", (iso(resumed_min),))
        db.commit()

    def test_counter_survives_pause_no_t0_arrival(self):
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [listing(server_id=5)])
            self._reopened(db, phase)
            for t in (300, 200):
                add_obs(db, 1, t, status(submitted=500))
            # the counter survived: the first post-resume reading is
            # still 500, so it is a flat delta -- not a 500-arrival
            add_obs(db, 1, 50, status(submitted=500))
            _, state = actions(db, phase)
            probe = state["rentals"][0]
            self.assertIsNotNone(probe["t0"])
            self.assertEqual(probe["share_arrivals"], [])
            # _found_share semantics: no arrival >= t0 means the
            # reactivated probe does not read as an instant finder
            self.assertFalse(any(
                ts >= probe["t0"]
                for ts, _ in probe["share_arrivals"]))
            add_obs(db, 1, 40, status(submitted=505))
            _, state = actions(db, phase)
            self.assertEqual(
                state["rentals"][0]["share_arrivals"],
                [(experiment._dt(iso(40)), 5)])
        finally:
            db.close()

    def test_counter_reset_counts_post_reset_value(self):
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [listing(server_id=5)])
            self._reopened(db, phase)
            for t in (300, 200):
                add_obs(db, 1, t, status(submitted=500))
            # container recreated: the counter restarted near zero
            add_obs(db, 1, 50, status(submitted=3))
            _, state = actions(db, phase)
            self.assertEqual(
                state["rentals"][0]["share_arrivals"],
                [(experiment._dt(iso(50)), 3)])
        finally:
            db.close()

    def test_unresumed_first_obs_still_counts_full(self):
        """resumed=None is unchanged: a fresh rental's first
        observation still counts its whole cumulative value."""
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [listing(server_id=5)])
            spot_rental(db, phase, 1, 5)
            add_obs(db, 1, 10, status(submitted=500))
            add_obs(db, 1, 5, status(submitted=505))
            _, state = actions(db, phase)
            self.assertEqual(
                state["rentals"][0]["share_arrivals"],
                [(experiment._dt(iso(10)), 500),
                 (experiment._dt(iso(5)), 5)])
        finally:
            db.close()


class SpotProbeTimingTests(unittest.TestCase):
    def _spot_probe_burst(self, db, phase, mode="spot"):
        burst_id = add_burst(db, phase["phase_id"], 2,
                             trigger="dry_retry", n_target=1)
        if mode == "spot":
            spot_rental(db, phase, 1, 5, 12, role="probe",
                        burst_id=burst_id)
        else:
            add_fleet(db, 1, 5)
            add_rental(db, phase["phase_id"], 1, 5, 12, role="probe",
                       burst_id=burst_id)
        add_obs(db, 1, 10, status(submitted=0))
        return burst_id

    def test_spot_probe_uses_spot_probe_judge_min(self):
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [])
            self._spot_probe_burst(db, phase)
            acts, _ = actions(db, phase)
            # t0 is 10m in the past: inside the 5m on-demand window,
            # short of the 20m spot window
            self.assertEqual(kinds(acts, "judge"), [])
            cfg = dict(CFG, spot_probe_judge_min=10)
            acts, _ = actions(db, phase, cfg=cfg)
            self.assertEqual(len(kinds(acts, "judge")), 1)
        finally:
            db.close()

    def test_ondemand_probe_window_unchanged(self):
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [])
            self._spot_probe_burst(db, phase, mode="on-demand")
            acts, _ = actions(db, phase)
            self.assertEqual(len(kinds(acts, "judge")), 1)
        finally:
            db.close()

    def test_spot_probe_exempt_from_probe_guard(self):
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [])
            spot_rental(db, phase, 1, 5, 5, role="probe")
            add_obs(db, 1, 4, status(submitted=0))
            acts, _ = actions(db, phase)
            self.assertNotIn("probe_guard",
                             {a.get("reason") for a in acts})
            # an on-demand probe at the same age sets the guard
            db.execute("UPDATE experiment_rentals SET mode='on-demand'"
                       " WHERE order_id=1")
            db.commit()
            acts, _ = actions(db, phase)
            self.assertIn("probe_guard",
                          {a.get("reason") for a in acts})
        finally:
            db.close()

    def test_spot_probe_deadline_scales_with_judge_min(self):
        """The hard cancel keeps the judge+cancel margin, not the
        on-demand free-window deadline."""
        db = make_db()
        try:
            phase = add_phase(db)
            add_snapshot(db, [])
            spot_rental(db, phase, 1, 5, 12, role="probe")
            add_obs(db, 1, 10, status(submitted=0))
            acts, _ = actions(db, phase)
            # 12min old: past the 8min on-demand deadline, inside the
            # 23min spot one
            self.assertEqual(kinds(acts, "cancel"), [])
        finally:
            db.close()


class SpotWatchTests(unittest.TestCase):
    def test_zero_api_calls_without_active_spot(self):
        # a bare db has no spot rentals and no spot inventory orders:
        # the watch must return before touching the API
        with mock.patch.object(
                cli.orders, "refresh_inventory",
                side_effect=AssertionError("API call")) as fetch, \
                mock.patch.object(
                    cli.spot, "refresh_quotes_for",
                    side_effect=AssertionError("API call")):
            rc = cli.spot_watch(":memory:", "/tmp/env", None,
                                say=lambda *_a: None)
        self.assertEqual(rc, 0)
        fetch.assert_not_called()

    def test_polls_with_active_spot_rental(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "db.sqlite3")
            db = experiment.open_db(db_path)
            phase = add_phase(db)
            spot_rental(db, phase, 1, 5)
            db.close()
            with mock.patch.dict(os.environ,
                                 {"CLORE_API_KEY": "KEY"}), \
                    mock.patch.object(
                        cli.orders, "refresh_inventory",
                        return_value=True) as refresh, \
                    mock.patch.object(
                        cli.spot, "refresh_quotes_for",
                        return_value=1) as quotes, \
                    mock.patch.object(
                        cli.spot, "latest_quotes", return_value={}):
                rc = cli.spot_watch(db_path, "/tmp/env", None,
                                    say=lambda *_a: None)
            self.assertEqual(rc, 0)
            refresh.assert_called_once()
            quotes.assert_called_once()
            self.assertEqual(quotes.call_args.args[1], [5])

    def test_inventory_fallback_detects_spot_orders(self):
        db = make_db()
        try:
            add_inventory_items(db, [{"order_id": 9, "server_id": 7,
                                      "spot": True},
                                     {"order_id": 1, "server_id": 5}])
            self.assertEqual(
                cli._active_spot(db),
                [{"order_id": 9, "server_id": 7, "bid_usd_day": None}])
        finally:
            db.close()

    def test_recent_outbid_row_stays_watched(self):
        """A row ended 'outbid' inside spot_outbid_min still feeds the
        watch -- that is what lets resume detection see the order at
        all; one past the window drops out."""
        db = make_db()
        try:
            phase = add_phase(db)
            spot_rental(db, phase, 1, 5)
            db.execute(
                "UPDATE experiment_rentals SET ended_at=?,"
                " end_reason='outbid' WHERE order_id=1", (iso(10),))
            db.commit()
            self.assertEqual(
                cli._active_spot(db, now=NOW),
                [{"order_id": 1, "server_id": 5,
                  "bid_usd_day": None}])
            db.execute(
                "UPDATE experiment_rentals SET ended_at=?"
                " WHERE order_id=1", (iso(70),))
            db.commit()
            self.assertEqual(cli._active_spot(db, now=NOW), [])
        finally:
            db.close()


class PreMigrationResumedAtTests(unittest.TestCase):
    """Pre-migration schema (no mode/resumed_at columns): read-only
    paths must still work -- the ALTER only runs in open_db."""

    def test_build_state_without_resumed_at_column(self):
        import market, fleet_store, pool_stats
        db = market.connect_db(":memory:")
        fleet_store.initialize(db)
        blacklist.initialize(db)
        pool_stats.initialize(db)
        db.executescript(experiment.SCHEMA)
        try:
            phase = add_phase(db)
            add_snapshot(db, [listing(server_id=5)])
            add_fleet(db, 1, 5)
            add_rental(db, phase["phase_id"], 1, 5, 30)
            cols = {r[1] for r in db.execute(
                "PRAGMA table_info(experiment_rentals)")}
            assert "resumed_at" not in cols and "mode" not in cols
            state = experiment.build_state(
                db, phase, NOW, CFG, apply_endings=False,
                say=lambda *_a: None)
            self.assertIsNone(state["rentals"][0]["resumed_at"])
            self.assertFalse(state["rentals"][0]["paused"])
            experiment.summary(db, NOW)
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
