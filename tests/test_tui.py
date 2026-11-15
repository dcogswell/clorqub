"""Tests for the TUI rent-flow progress view: Hide only hides the
log -- the rental worker must keep running, must not be cancelled by
the periodic reload, and `b` must reopen the live log."""

import asyncio
import json
import os
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import tui  # noqa: E402


TS = "2026-11-15T00:00:00+00:00"


def seed_dashboard_db(path):
    """A minimal but complete dashboard db: one experiment rental, one
    event, one unmanaged Clore order."""
    import experiment
    import orders
    import spot
    db = experiment.open_db(path)
    db.executescript(orders.SCHEMA)
    db.executescript(spot.SCHEMA)
    db.execute(
        "INSERT INTO marketplace_snapshots(captured_at, source,"
        " server_count, raw_json) VALUES (?,?,?,?)",
        (TS, "test", 0, json.dumps({"servers": []})))
    db.execute(
        "INSERT INTO fleet_rentals(order_id, server_id, alias, pool,"
        " ssh_key_path, provision_state, created_at, updated_at)"
        " VALUES (10, 20, 'rig', 'jetski', '/k', 'ready', ?, ?)",
        (TS, TS))
    db.execute(
        "INSERT INTO experiment_phases(started_at, ends_at,"
        " budget_usd, reserve_usd, config_json) VALUES (?,?,?,?,?)",
        (TS, "2099-01-01T00:00:00+00:00", "5", "0.50",
         json.dumps(experiment.DEFAULT_CONFIG)))
    db.execute(
        "INSERT INTO experiment_rentals(order_id, phase_id, server_id,"
        " tier, reason, x_min, y_min, hourly_usd, creation_fee_usd,"
        " quoted_cost_usd, created_at, role) VALUES"
        " (10, 1, 20, 'zen', 'fill', 30, 15, '0.10', '0.10', '0.70',"
        " ?, 'slot')", (TS,))
    db.execute(
        "INSERT INTO experiment_events(ts, phase_id, order_id,"
        " server_id, kind, detail_json) VALUES (?,1,10,20,'rent',?)",
        (TS, json.dumps({"reason": "fill", "tier": "zen"})))
    recs = [{"order_id": 10, "server_id": 20, "spot": False,
             "price_day_native": 4.8, "spend_reported_native": 1.23,
             "online_reported": True, "control": "experiment",
             "gpus": ["RTX 3090"], "currency": "USD-Blockchain"},
            {"order_id": 500, "server_id": 50, "spot": False,
             "price_day_native": 2.4, "spend_reported_native": 0.5,
             "online_reported": True, "control": "web",
             "gpus": ["RTX 4090"], "currency": "USD-Blockchain"}]
    db.execute(
        "INSERT INTO order_inventory(captured_at, ok, orders_json)"
        " VALUES (?,1,?)", ("2099-01-01T00:00:00+00:00",
                           json.dumps(recs)))
    db.commit()
    db.close()


async def wait_dash(app, pilot):
    for _ in range(200):
        await pilot.pause()
        if app.dash is not None:
            return
    raise AssertionError("dashboard never loaded")


def make_app():
    # a nonexistent db file is fine: reload_data reports a load error
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(db_path)
    return tui.MarketTui(db_path=db_path, refresh=3600)


def blocking_rent_module(done_event):
    """Fake `rent` module: create_order_flow blocks until released."""
    fake = types.SimpleNamespace()

    def create_order_flow(ctx, say=None, warn=None):
        done_event.wait(timeout=30)
        return (0, {"order_id": 1, "provision_state": "READY",
                    "stage": "done"})

    fake.create_order_flow = create_order_flow
    return fake


def rent_worker(app):
    return next(
        (w for w in app.workers if w.group == "rent"), None)


class RentWorkerLifetimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_reload_does_not_cancel_rent_worker(self):
        done = threading.Event()
        fake = blocking_rent_module(done)
        try:
            with mock.patch.dict(sys.modules, {"rent": fake}):
                app = make_app()
                async with app.run_test() as pilot:
                    app._rent_active = True
                    app._rent_confirmed(ctx={})
                    await pilot.pause()
                    worker = rent_worker(app)
                    self.assertIsNotNone(worker)
                    # periodic reloads share the default exclusive
                    # group; they must not touch the rent group
                    app.reload_data()
                    await pilot.pause()
                    app.reload_data()
                    await pilot.pause()
                    self.assertFalse(worker.is_cancelled)
                    self.assertIs(rent_worker(app), worker)
                    done.set()
                    for _ in range(100):
                        await pilot.pause()
                        if not app._rent_active:
                            break
                    self.assertFalse(app._rent_active)
        finally:
            done.set()


class HideReopenQuitTests(unittest.IsolatedAsyncioTestCase):
    async def test_hide_reopen_and_quit_blocked(self):
        done = threading.Event()
        fake = blocking_rent_module(done)
        try:
            with mock.patch.dict(sys.modules, {"rent": fake}):
                app = make_app()
                async with app.run_test() as pilot:
                    app._rent_active = True
                    app._rent_confirmed(ctx={})
                    await pilot.pause()
                    modal = app._rent_modal
                    self.assertIsInstance(modal, tui.ProgressModal)

                    # Hide: Escape just dismisses the view
                    await pilot.press("escape")
                    await pilot.pause()
                    self.assertFalse(
                        any(isinstance(s, tui.ProgressModal)
                            for s in app.screen_stack))
                    self.assertTrue(app._rent_active)
                    self.assertTrue(app.is_running)

                    # `q` while hidden must not quit mid-rental
                    await pilot.press("q")
                    await pilot.pause()
                    self.assertTrue(app.is_running)
                    self.assertIsNone(app.return_code)
                    self.assertTrue(app._rent_active)

                    # `b` reopens a fresh modal preloaded with the log
                    await pilot.press("b")
                    await pilot.pause()
                    top = app.screen_stack[-1]
                    self.assertIsInstance(top, tui.ProgressModal)
                    self.assertIsNot(top, modal)
                    self.assertIs(app._rent_modal, top)
                    self.assertTrue(
                        any("confirmed -- re-verifying" in line
                            for line in top._lines))

                    done.set()
                    for _ in range(100):
                        await pilot.pause()
                        if not app._rent_active:
                            break
                    self.assertFalse(app._rent_active)
        finally:
            done.set()


class ExperimentTabTests(unittest.IsolatedAsyncioTestCase):
    async def test_blacklisted_manual_review_still_allows_confirmation(self):
        from textual.widgets import Static
        app = make_app()
        confirmed = []
        ctx = {"server_id": 12, "quote": {}, "server": {"specs": {}},
               "pool": "jetski", "alias": "rig-test", "key_path": "/unused",
               "blacklist": {"reason": "flaky_ssh", "source": "auto",
                             "expires_at": "2026-11-18T00:00:00+00:00"}}
        async with app.run_test() as pilot:
            modal = tui.ReviewModal({}, ctx, on_confirm=confirmed.append)
            app.push_screen(modal)
            await pilot.pause()
            content = " ".join(str(w.render()) for w in modal.query(Static))
            self.assertIn("flaky_ssh", content)
            self.assertIn("manual rental allowed", content)
            modal._confirm()
            self.assertEqual(confirmed, [ctx])

    async def test_experiment_tab_renders(self):
        from textual.widgets import DataTable, Static, TabbedContent
        from textual.css.query import NoMatches

        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "dash.sqlite3")
            seed_dashboard_db(db_path)
            app = tui.MarketTui(db_path=db_path, refresh=3600)
            async with app.run_test() as pilot:
                await wait_dash(app, pilot)
                tabs = app.query_one(TabbedContent)
                tabs.active = "experiment"
                await pilot.pause()

                status = str(app.query_one("#exp-status", Static)
                             .render())
                self.assertIn("phase 1", status)
                self.assertIn("budget $5.00", status)
                self.assertIn("runner:", status)

                table = app.query_one("#exp-table", DataTable)
                self.assertEqual(len(table.rows), 1)
                row = table.get_row_at(0)
                cells = [str(c) for c in row]
                self.assertIn("10", cells[0])   # order
                self.assertIn("zen", cells)

                log = str(app.query_one("#exp-log", Static)
                          .render())
                self.assertIn("rent", log)
                self.assertIn("reason=fill", log)

                # Orders tab is gone
                with self.assertRaises(NoMatches):
                    app.query_one("#orders-table")

                # Fleet gained the Clore order columns
                fleet = app.query_one("#fleet-table", DataTable)
                labels = [c.label.plain for c in
                          fleet.columns.values()]
                for col in ("Sh/¢", "Spend$", "Online", "Control"):
                    self.assertIn(col, labels)
                # unmanaged order 500 renders as a fleet row
                keys = {r.key.value for r in fleet.ordered_rows}
                self.assertIn("500", keys)
                row_idx = [i for i, r in enumerate(fleet.ordered_rows)
                           if r.key.value == "500"][0]
                cells = [str(c) for c in fleet.get_row_at(row_idx)]
                self.assertIn("unmanaged", cells)


if __name__ == "__main__":
    unittest.main()
