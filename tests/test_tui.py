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
from datetime import datetime, timedelta, timezone
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


def right_click(widget, x, y):
    """Post the down/up/click triplet a real right-click produces.
    (pilot.click can't hit-test widgets in inactive TabPanes: their
    `region` stays 0x0 in headless tests.)"""
    from textual import events
    widget.post_message(events.MouseDown(
        widget, x, y, 0, 0, 3, False, False, False))
    widget.post_message(events.MouseUp(
        widget, x, y, 0, 0, 3, False, False, False))
    widget.post_message(events.Click(
        widget, x, y, 0, 0, 3, False, False, False))


class CandidateSortTests(unittest.TestCase):
    """Default ordering follows the runner's auto_prior (forecast
    return / value per $) when a live phase exports it, otherwise the
    legacy est its/$ order; 's' toggles $/day asc."""

    def _app(self, rows):
        app = make_app()
        app.dash = {"rows": rows}
        return app

    def test_default_sort_its_per_usd_desc(self):
        rows = [
            {"server_id": 1, "est_its_per_usd": 50,
             "estimated_sols_per_usd": 900},
            {"server_id": 2, "est_its_per_usd": 300,
             "estimated_sols_per_usd": 100},
            {"server_id": 3, "estimated_sols_per_usd": 9999},
            {"server_id": 4, "est_its_per_usd": 100},
        ]
        app = self._app(rows)
        self.assertEqual(
            [r["server_id"] for r in app._filtered()], [2, 4, 1, 3])

    def test_auto_prior_sort_overrides_raw_ratio(self):
        """The runner's ranking wins over the raw static ratios once
        any row carries auto_prior."""
        rows = [
            {"server_id": 1, "est_its_per_usd": 999,
             "estimated_sols_per_usd": 999,
             "auto_prior": 1.0, "hourly_cost_usd": 0.10},
            {"server_id": 2, "est_its_per_usd": 10,
             "auto_prior": 5.0, "hourly_cost_usd": 0.20},
            {"server_id": 3, "auto_prior": None,
             "hourly_cost_usd": 0.01},
            {"server_id": 4, "est_its_per_usd": 500,
             "hourly_cost_usd": 0.02},
        ]
        app = self._app(rows)
        self.assertEqual(
            [r["server_id"] for r in app._filtered()], [2, 1, 3, 4])

    def test_auto_prior_tiebreaks_price_then_id(self):
        rows = [
            {"server_id": 9, "auto_prior": 3.0,
             "hourly_cost_usd": 0.30},
            {"server_id": 7, "auto_prior": 3.0,
             "hourly_cost_usd": 0.10},
            {"server_id": 8, "auto_prior": 3.0,
             "hourly_cost_usd": 0.10},
        ]
        app = self._app(rows)
        self.assertEqual(
            [r["server_id"] for r in app._filtered()], [7, 8, 9])

    def test_price_toggle_still_works(self):
        rows = [
            {"server_id": 1, "effective_price_day_usd": 9.0},
            {"server_id": 2, "effective_price_day_usd": 3.0},
            {"server_id": 3},
        ]
        app = self._app(rows)
        app.sort_by_price = True
        self.assertEqual(
            [r["server_id"] for r in app._filtered()], [2, 1, 3])


class AutofitTests(unittest.IsolatedAsyncioTestCase):
    """auto_width columns must re-fit current content every fill --
    clear() keeps Column.content_width, which only grows via max(),
    so without the reset a single wide cell pins the column forever."""

    async def test_column_shrinks_after_wide_cell(self):
        from textual.widgets import DataTable
        app = make_app()
        async with app.run_test() as pilot:
            cand = app.query_one("#cand", DataTable)
            app._fill(cand, [{"server_id": 1,
                              "cpu": "X" * 60}], tui.CAND_COLS,
                      "cand")
            await pilot.pause()
            await pilot.pause()
            wide = max(c.content_width
                       for c in cand.columns.values())
            self.assertGreaterEqual(wide, 60)
            app._fill(cand, [{"server_id": 2, "cpu": "X"}],
                      tui.CAND_COLS, "cand")
            await pilot.pause()
            await pilot.pause()
            cpu_col = next(c for c in cand.columns.values()
                           if c.label.plain == "CPU model")
            self.assertLess(cpu_col.content_width, 60)


class CandidateCellTests(unittest.TestCase):
    """The Candidates forecast columns mirror the runner's scored
    pool: dollar estimates when the value model is live, '-' without
    a runner pool."""

    def test_forecast_cells(self):
        cells = make_app()._cells({
            "server_id": 1, "forecast_usd_hr": 0.258,
            "forecast_return_x": 3.62, "auto_prior": 3.62,
            "value_fill_status": "value ok"})
        self.assertEqual(str(cells["Est$/h"]), "~$0.258")
        self.assertEqual(str(cells["EstRet"]), "~3.62x")
        self.assertEqual(str(cells["Auto"]), "value ok")

    def test_relative_mode_cells(self):
        cells = make_app()._cells({
            "server_id": 1, "auto_prior": 144.5,
            "forecast_return_x": None, "forecast_usd_hr": None,
            "value_fill_status": "fill pace"})
        self.assertEqual(str(cells["Est$/h"]), "-")
        self.assertEqual(str(cells["EstRet"]), "-")
        self.assertEqual(str(cells["Auto"]), "fill pace")

    def test_no_runner_fields(self):
        cells = make_app()._cells({"server_id": 1})
        for col in ("Est$/h", "EstRet", "Auto"):
            self.assertEqual(str(cells[col]), "-")


class CandidateTabTests(unittest.IsolatedAsyncioTestCase):
    async def test_candidates_render_runner_pool_values(self):
        """End-to-end: a live phase's candidate_values drive the
        Candidates tab membership and forecast columns."""
        from textual.widgets import DataTable
        from test_experiment import (
            listing, add_history)
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "dash.sqlite3")
            seed_dashboard_db(db_path)
            import experiment
            db = experiment.open_db(db_path)
            db.execute(
                "UPDATE marketplace_snapshots SET raw_json=?",
                (json.dumps({"servers": [
                    listing(30),
                    listing(31, reliability=0.5)]}),))
            # a big budget keeps pace out of the Auto status, so the
            # rendering assertions don't depend on epoch distance
            db.execute(
                "UPDATE experiment_phases SET budget_usd='1000'")
            add_history(db, 30, fleet_order=9000,
                        its_cpu=10, its_gpu=80)
            # ~9 days back: unambiguously prior-epoch for the real
            # clock no matter which week the suite runs in
            old = (datetime.now(timezone.utc)
                   - timedelta(days=9)).isoformat()
            db.execute(
                "UPDATE rig_observations SET captured_at=?", (old,))
            db.commit()
            db.close()
            app = tui.MarketTui(db_path=db_path, refresh=3600)
            async with app.run_test() as pilot:
                await wait_dash(app, pilot)
                cand = app.query_one("#cand", DataTable)
                labels = [c.label.plain for c in
                          cand.columns.values()]
                for col in ("Est$/h", "EstRet", "Auto"):
                    self.assertIn(col, labels)
                for col in ("sols/h", "ODsol/$", "it/$"):
                    self.assertNotIn(col, labels)
                keys = {r.key.value for r in cand.ordered_rows}
                # s30 is in the runner pool; s31 fails the runner's
                # reliability gate and stays in the Marketplace
                self.assertIn("30", keys)
                self.assertNotIn("31", keys)
                mkt = app.query_one("#mkt", DataTable)
                self.assertIn("31", {r.key.value
                                     for r in mkt.ordered_rows})
                idx = [i for i, r in enumerate(cand.ordered_rows)
                       if r.key.value == "30"][0]
                cells = [str(c) for c in cand.get_row_at(idx)]
                # obs are seeded against the fixed test NOW, prior
                # epoch for the real clock: the * marks cross-epoch
                self.assertEqual(
                    cells[labels.index("est it/s")], "90*")
                # relative mode (no pool snapshots): no dollar
                # forecast, but a runner fill status
                self.assertEqual(cells[labels.index("Est$/h")], "-")
                self.assertNotEqual(cells[labels.index("Auto")], "-")


class EpHeaderTests(unittest.IsolatedAsyncioTestCase):
    EP_KEYS = ("epoch_plan_usd", "epoch_plan_additional_usd",
               "epoch_plan_profit_usd", "epoch_plan_phase_limited")

    def test_ep_header_format(self):
        # pure formatting: the runner supplies 2dp strings or None
        self.assertEqual(
            tui._ep_header({"epoch_plan_usd": "6.00",
                            "epoch_plan_additional_usd": "2.00"}),
            "EP ~$6.00 (+$2.00)")
        self.assertEqual(
            tui._ep_header({"epoch_plan_usd": "6.00",
                            "epoch_plan_additional_usd": "0.00"}),
            "EP ~$6.00 (+$0.00)")
        # an older runner without the shortfall key still shows cost
        self.assertEqual(
            tui._ep_header({"epoch_plan_usd": "6.00"}), "EP ~$6.00")
        self.assertEqual(tui._ep_header({}), "EP -")

    async def test_ep_header_renders(self):
        """Header surfaces the net-profit epoch plan: cost, funded
        shortfall, projected profit, and the phase-extension flag."""
        from textual.widgets import Static
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "dash.sqlite3")
            seed_dashboard_db(db_path)
            app = tui.MarketTui(db_path=db_path, refresh=3600)
            async with app.run_test() as pilot:
                await wait_dash(app, pilot)
                phase = (app.dash.setdefault("experiment", {})
                         .setdefault("phase", {}))
                for k in self.EP_KEYS:
                    phase.pop(k, None)
                h2 = app.query_one("#h2", Static)
                h3 = app.query_one("#h3", Static)

                phase.update({"epoch_plan_usd": "6.00",
                              "epoch_plan_additional_usd": "2.00",
                              "epoch_plan_profit_usd": "9.00"})
                app.update_header()
                self.assertIn("EP ~$6.00 (+$2.00)",
                              str(h2.render()))
                self.assertIn("EP profit~$9.00", str(h3.render()))

                # already funded plans still quote the shortfall
                # explicitly, and a phase ending before the epoch is
                # flagged rather than silently extended
                phase.update({"epoch_plan_additional_usd": "0.00",
                              "epoch_plan_phase_limited": True})
                app.update_header()
                self.assertIn("EP ~$6.00 (+$0.00)",
                              str(h2.render()))
                self.assertIn("EP needs phase extension",
                              str(h3.render()))

                # no fresh calibration: no dollar projection, and the
                # extension flag cannot appear without a plan cost
                for k in self.EP_KEYS:
                    phase.pop(k, None)
                phase.update({"epoch_plan_usd": None,
                              "epoch_plan_phase_limited": True})
                app.update_header()
                self.assertIn("EP -", str(h2.render()))
                self.assertNotIn("EP ~$", str(h2.render()))
                self.assertNotIn("EP profit", str(h3.render()))
                self.assertNotIn("EP needs phase extension",
                                 str(h3.render()))


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

                from textual.widgets import TextArea
                log = app.query_one("#exp-log", TextArea).text
                self.assertIn("rent", log)
                self.assertIn("fill", log)

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

    async def test_spot_mode_columns(self):
        """Mode columns mark spot rentals/rigs 'spot', else '-':
        the experiment tab reads the rental's stored mode, the fleet
        tab maps the last good inventory's spot flag by order_id."""
        from textual.widgets import DataTable, TabbedContent
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "dash.sqlite3")
            seed_dashboard_db(db_path)
            import experiment
            db = experiment.open_db(db_path)
            db.execute("UPDATE experiment_rentals SET mode='spot'")
            inv = db.execute(
                "SELECT id, orders_json FROM order_inventory"
                " ORDER BY id DESC LIMIT 1").fetchone()
            recs = json.loads(inv["orders_json"])
            for r in recs:
                if r["order_id"] == 10:
                    r["spot"] = True
            db.execute("UPDATE order_inventory SET orders_json=?"
                       " WHERE id=?", (json.dumps(recs), inv["id"]))
            db.commit()
            db.close()
            app = tui.MarketTui(db_path=db_path, refresh=3600)
            async with app.run_test() as pilot:
                await wait_dash(app, pilot)
                app.query_one(TabbedContent).active = "experiment"
                await pilot.pause()

                table = app.query_one("#exp-table", DataTable)
                labels = [c.label.plain
                          for c in table.columns.values()]
                self.assertIn("Mode", labels)
                cells = [str(c) for c in table.get_row_at(0)]
                self.assertEqual(cells[labels.index("Mode")], "spot")

                fleet = app.query_one("#fleet-table", DataTable)
                labels = [c.label.plain
                          for c in fleet.columns.values()]
                self.assertIn("Mode", labels)
                rows = {r.key.value: i
                        for i, r in enumerate(fleet.ordered_rows)}
                cells = [str(c)
                         for c in fleet.get_row_at(rows["10"])]
                self.assertEqual(cells[labels.index("Mode")], "spot")
                cells = [str(c)
                         for c in fleet.get_row_at(rows["500"])]
                self.assertEqual(cells[labels.index("Mode")], "-")

    async def test_exp_log_copy(self):
        """ctrl+c and right-click in the exp log must reach the real
        clipboard tools -- OSC 52 alone is dropped by tmux/VTE."""
        from textual.widgets import TabbedContent
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "dash.sqlite3")
            seed_dashboard_db(db_path)
            app = tui.MarketTui(db_path=db_path, refresh=3600)
            copied = []
            with mock.patch.object(
                    tui.clipboard, "copy_text",
                    side_effect=lambda t: copied.append(t) or "xclip"):
                async with app.run_test() as pilot:
                    await wait_dash(app, pilot)
                    app.query_one(TabbedContent).active = "experiment"
                    await pilot.pause()
                    log = app.query_one("#exp-log", tui.LogTextArea)
                    line0 = log.document[0]

                    # ctrl+c copies the selection
                    log.selection = tui.Selection(
                        (0, 0), (0, len(line0)))
                    log.action_copy()
                    await pilot.pause()
                    self.assertEqual(copied, [line0])
                    self.assertEqual(app._clipboard_tool, "xclip")

                    # right-click copies the existing selection and
                    # does not reset it first
                    copied.clear()
                    right_click(log, 2, 1)
                    await pilot.pause()
                    self.assertEqual(copied, [line0])
                    self.assertEqual(
                        log.selection, tui.Selection((0, 0),
                                                     (0, len(line0))))

                    # right-click with no selection copies the
                    # clicked line
                    copied.clear()
                    log.selection = tui.Selection((0, 0), (0, 0))
                    right_click(log, 2, 0)
                    await pilot.pause()
                    self.assertEqual(copied, [line0])

                    # ctrl+c with no selection does not clobber the
                    # clipboard with an empty string
                    copied.clear()
                    log.selection = tui.Selection((0, 0), (0, 0))
                    log.action_copy()
                    await pilot.pause()
                    self.assertEqual(copied, [])

    async def test_exp_log_shows_all_detail_and_notes(self):
        """Event details render in full (flattened), and `note`
        decision events show their text."""
        from textual.widgets import TabbedContent
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "dash.sqlite3")
            seed_dashboard_db(db_path)
            import experiment
            db = experiment.open_db(db_path)
            db.execute(
                "INSERT INTO experiment_events(ts, phase_id, order_id,"
                " server_id, kind, detail_json) VALUES (?,1,NULL,NULL,"
                " 'note', ?)",
                (TS, json.dumps(
                    {"text": "fill: 3 eligible -> pick s20"})))
            db.execute(
                "INSERT INTO experiment_events(ts, phase_id, order_id,"
                " server_id, kind, detail_json) VALUES (?,1,10,20,"
                " 'blocked', ?)",
                (TS, json.dumps(
                    {"reason": "balance", "balance": "1.0",
                     "need": "5.0",
                     "info": {"stage": "provision",
                              "state": "queued"}})))
            db.commit()
            db.close()
            app = tui.MarketTui(db_path=db_path, refresh=3600)
            async with app.run_test() as pilot:
                await wait_dash(app, pilot)
                app.query_one(TabbedContent).active = "experiment"
                await pilot.pause()
                log = app.query_one("#exp-log", tui.LogTextArea).text
                self.assertIn("note fill: 3 eligible -> pick s20", log)
                self.assertIn("balance=1.0", log)
                self.assertIn("need=5.0", log)
                self.assertIn("stage=provision", log)  # nested -> flat

    async def test_exp_log_lines_are_colored(self):
        """#exp-log renders through the log colorizer (timestamps dim,
        kinds/ids tinted) while the document stays plain text."""
        from textual.widgets import TabbedContent
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "dash.sqlite3")
            seed_dashboard_db(db_path)
            app = tui.MarketTui(db_path=db_path, refresh=3600)
            async with app.run_test() as pilot:
                await wait_dash(app, pilot)
                app.query_one(TabbedContent).active = "experiment"
                await pilot.pause()
                log = app.query_one("#exp-log", tui.LogTextArea)
                line = log.get_line(0)
                self.assertIn("rent #10 s20", line.plain)
                styled = {line.plain[s.start:s.end]: str(s.style)
                          for s in line.spans}
                self.assertEqual(styled.get("rent"), "#4ade80")
                self.assertEqual(styled.get("#10"), "#bb9af7")
                self.assertEqual(styled.get("s20"), "#bb9af7")
                self.assertEqual(
                    styled.get(line.plain[:8]), "#6b7280")  # HH:MM:SS

    async def test_upgrade_check_pin_and_value_cells(self):
        """The pinned line is the newest 'upgrade scan:' note rendered
        by the same timestamped _ev_line as the log, and the table
        shows the runner's smoothed it/s and provisional EstRet."""
        from textual.widgets import DataTable, Static, TabbedContent
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "dash.sqlite3")
            seed_dashboard_db(db_path)
            app = tui.MarketTui(db_path=db_path, refresh=3600)
            async with app.run_test() as pilot:
                await wait_dash(app, pilot)
                app.query_one(TabbedContent).active = "experiment"
                await pilot.pause()
                exp = app.dash["experiment"]
                # newest-first events: a newer non-scan note must not
                # shadow the pin, and of two scan notes the newer wins
                exp["events"] = [
                    {"ts": "2026-11-15T02:00:00+00:00",
                     "kind": "note",
                     "detail": {"text": "fill pace: all quiet"}},
                    {"ts": "2026-11-15T01:30:00+00:00",
                     "kind": "note",
                     "detail": {"text": (
                         "upgrade scan: best s121971 est 2.874 ret "
                         "vs worst 2.665 (need >3.331)")}},
                    {"ts": "2026-11-15T01:00:00+00:00",
                     "kind": "note",
                     "detail": {"text": (
                         "upgrade scan: s121971 dominant but est "
                         "$0.40 > remaining $0.10")}},
                ]
                exp["rentals"][0].update(
                    {"its_total": 89.6, "forecast_return_x": 2.874})
                app.render_experiment()
                pinned = str(
                    app.query_one("#exp-upgrade", Static).render())
                self.assertEqual(
                    pinned, "Latest upgrade check: "
                    + tui._ev_line(exp["events"][1]))
                self.assertIn("need >3.331", pinned)
                self.assertNotIn("all quiet", pinned)
                self.assertNotIn("dominant but est", pinned)

                table = app.query_one("#exp-table", DataTable)
                labels = [c.label.plain
                          for c in table.columns.values()]
                self.assertEqual(labels.index("it/s"),
                                 labels.index("$/hr") + 1)
                self.assertEqual(labels.index("EstRet"),
                                 labels.index("$/hr") + 2)
                cells = [str(c) for c in table.get_row_at(0)]
                self.assertEqual(cells[labels.index("it/s")], "90")
                self.assertEqual(
                    cells[labels.index("EstRet")], "~2.87x")

                # a known zero speed is still shown; no calibration
                # renders a dash rather than a fabricated return
                exp["rentals"][0].update(
                    {"its_total": 0, "forecast_return_x": None})
                app.render_experiment()
                cells = [str(c) for c in table.get_row_at(0)]
                self.assertEqual(cells[labels.index("it/s")], "0")
                self.assertEqual(cells[labels.index("EstRet")], "-")
                exp["rentals"][0]["its_total"] = None
                app.render_experiment()
                cells = [str(c) for c in table.get_row_at(0)]
                self.assertEqual(cells[labels.index("it/s")], "-")

                # placeholder with no scan note; cleared with no phase
                exp["events"] = []
                app.render_experiment()
                self.assertEqual(
                    str(app.query_one("#exp-upgrade", Static)
                        .render()),
                    "Upgrade check: no assessment yet")
                exp["phase"] = None
                app.render_experiment()
                self.assertEqual(
                    str(app.query_one("#exp-upgrade", Static)
                        .render()), "")


class EventLineTests(unittest.TestCase):
    """_ev_line renders each event as a plain-English sentence and
    keeps any detail the sentence didn't cover as residual k=v.
    Timestamps render in local time."""

    TS = "2026-11-15T12:34:56+00:00"
    HHMMSS = datetime.fromisoformat(TS).astimezone().strftime(
        "%H:%M:%S")

    def ev(self, kind, detail=None, order_id=None, server_id=None):
        return {"ts": self.TS, "kind": kind,
                "order_id": order_id, "server_id": server_id,
                "detail": detail or {}}

    def test_blocked_reason_in_plain_english(self):
        line = tui._ev_line(self.ev(
            "blocked", {"reason": "telemetry_stale"}))
        self.assertEqual(line,
                         f"{self.HHMMSS} blocked: rig telemetry stale")
        line = tui._ev_line(self.ev(
            "blocked", {"reason": "one_rent_per_cycle"}, order_id=9,
            server_id=3))
        self.assertEqual(
            line, f"{self.HHMMSS} blocked #9 s3:"
                  " deferred: 1 rental per cycle")
        # structured extras remain as residual fields
        line = tui._ev_line(self.ev(
            "blocked", {"reason": "balance", "balance": "1.0",
                        "need": "5.0"}))
        self.assertIn("wallet balance too low", line)
        self.assertIn("balance=1.0", line)
        self.assertIn("need=5.0", line)

    def test_cancel_shows_reason_and_final_stats(self):
        line = tui._ev_line(self.ev(
            "cancel", {"reason": "probe_deadline", "its": 0,
                       "shares": 0, "age_min": 8},
            order_id=42, server_id=7))
        self.assertEqual(
            line, f"{self.HHMMSS} cancel #42 s7: probe hit free-window"
                  " deadline  its=0 shares=0 age_min=8")

    def test_unknown_reason_passes_through(self):
        line = tui._ev_line(self.ev(
            "cancel", {"reason": "brand_new_reason"}, order_id=1))
        self.assertIn("brand_new_reason", line)

    def test_rent_and_burst_lines(self):
        line = tui._ev_line(self.ev(
            "rent", {"reason": "fill", "tier": "zen", "role": "slot",
                     "hourly_usd": "0.10"},
            order_id=10, server_id=20))
        self.assertEqual(
            line, f"{self.HHMMSS} rent #10 s20 zen/slot (fill)"
                  "  hourly_usd=0.10")
        line = tui._ev_line(self.ev(
            "burst_started", {"burst_id": 3, "trigger": "upgrade",
                              "n": 4, "y_min": 8}))
        self.assertEqual(
            line, f"{self.HHMMSS} burst #3 started (upgrade n=4 y=8m)")

    def test_ended_and_first_share(self):
        line = tui._ev_line(self.ev(
            "ended", {"reason": "term_end", "its": 80, "shares": 7,
                      "age_min": 60}, order_id=1, server_id=11))
        self.assertEqual(
            line, f"{self.HHMMSS} ended #1 s11: rental term ended"
                  "  its=80 shares=7 age_min=60")
        line = tui._ev_line(self.ev(
            "first_share", {"age_min": 4.2, "role": "probe", "its": 90},
            order_id=2, server_id=12))
        self.assertEqual(
            line, f"{self.HHMMSS} first share #2 s12 after 4.2m"
                  "  role=probe its=90")

    def test_tick_heartbeat_line(self):
        line = tui._ev_line(self.ev("tick", {
            "pulse": 1, "committed": "4.30", "remaining": "5.70",
            "burn_hr": "0.227", "optimal_hr": "0.131",
            "rigs": "2446554:slot mining its=217 | 2446566:slot "
                    "mining its=95"}))
        self.assertEqual(
            line, f"{self.HHMMSS} alive — committed=$4.30 "
                  "remaining=$5.70 burn=$0.227/h opt=$0.131/h"
                  "  |  2446554:slot mining its=217 | 2446566:slot "
                  "mining its=95")

    def test_rental_value_line(self):
        # pre-method events render as legacy allocations and are
        # never called measured
        line = tui._ev_line(self.ev(
            "rental_value", {"shares": 1893.0, "weighted": 937.4,
                             "usd_est": 1.38, "cost_usd": 0.36,
                             "return_x": 3.8,
                             "pre_snapshot_shares": 1893.0},
            order_id=7, server_id=42))
        self.assertEqual(
            line, f"{self.HHMMSS} value estimate #7 s42: "
                  "1,893 shares -> 937 weighted (legacy allocation)"
                  " ~ $1.38 vs $0.36 cost (3.8x)"
                  "  pre_snapshot_shares=1893")
        # new events carry the wallet-average method label
        line = tui._ev_line(self.ev(
            "rental_value", {"shares": 1893.0, "weighted": 937.4,
                             "usd_est": 1.38, "cost_usd": 0.36,
                             "return_x": 3.8,
                             "method": "wallet_average",
                             "pre_snapshot_shares": 1893.0},
            order_id=7, server_id=42))
        self.assertEqual(
            line, f"{self.HHMMSS} value estimate #7 s42: "
                  "1,893 shares -> 937 weighted (wallet-average allocation)"
                  " ~ $1.38 vs $0.36 cost (3.8x)"
                  "  pre_snapshot_shares=1893")


class LogColorizeTests(unittest.TestCase):
    STATUS = ("2026-11-14T15:59:56.504+00:00 QUBJETSKI "
              "2026-11-14 15:59:56.504 [INFO]  E:239 | "
              "SHARES: 0/0 (R:0) | [AVX2] 1 it/s | [CUDA] 84 it/s | "
              "78 avg it/s")

    def spans(self, text):
        t = tui.colorize_log(text)
        return t, [(s.start, s.end, str(s.style)) for s in t.spans]

    def test_text_preserved_and_tokens_colored(self):
        t, spans = self.spans(self.STATUS)
        self.assertEqual(t.plain, self.STATUS)
        styles = {style for _, _, style in spans}
        self.assertIn("#2dd4bf", styles)   # QUBJETSKI, [AVX2], [CUDA]
        self.assertIn("#4ade80", styles)   # it/s rates
        self.assertIn("#bb9af7", styles)   # E:239 epoch
        self.assertIn("#6b7280", styles)   # timestamps, [INFO], R:0

    def test_shares_found_green_and_rejects_red(self):
        t, spans = self.spans("SHARES: 3/3 (R:2)")
        styled = {t.plain[s:e]: style for s, e, style in spans}
        self.assertEqual(styled.get("3/3"), "#4ade80")
        self.assertEqual(styled.get(" (R:2)"), "#f87171")

    def test_error_line_tinted(self):
        t, spans = self.spans(
            "14:23:45 POST failed: connection refused")
        styled = {t.plain[s:e]: style for s, e, style in spans}
        self.assertEqual(styled.get(" POST "), "#f87171")
        self.assertEqual(styled.get("failed"), "#f87171")
        self.assertEqual(styled.get("14:23:45"), "#6b7280")

    def test_warn_line_tinted(self):
        t, spans = self.spans("2026-11-14T00:00:00 [WARN] low share rate")
        styled = {t.plain[s:e]: style for s, e, style in spans}
        self.assertEqual(styled.get("[WARN]"), "#f59e0b")
        self.assertEqual(styled.get(" low share rate"), "#f59e0b")

    def test_ansi_stripped(self):
        t, _ = self.spans("\x1b[31m[INFO] hi\x1b[0m")
        self.assertEqual(t.plain, "[INFO] hi")


class StrategyModalTests(unittest.IsolatedAsyncioTestCase):
    """loss_mining checked greys out the gate fields it overrides;
    margins and the price cap stay editable."""

    async def test_loss_mining_dims_inert_gate_fields(self):
        from textual.widgets import Checkbox, Input
        app = make_app()
        async with app.run_test() as pilot:
            app.push_screen(tui.StrategyModal({
                "score": "gross", "loss_mining": True,
                "min_return": 1.2, "min_value_ratio": 0.7}))
            await pilot.pause()
            modal = app.screen
            for k in ("min_value_ratio", "min_return"):
                self.assertTrue(
                    modal.query_one(f"#st-{k}", Input).disabled, k)
            self.assertTrue(
                modal.query_one("#st-gates").has_class("gate-off"))
            for k in ("upgrade_margin", "promote_margin",
                      "max_price_hr"):
                self.assertFalse(
                    modal.query_one(f"#st-{k}", Input).disabled, k)
            modal.query_one("#st-loss", Checkbox).value = False
            await pilot.pause()
            self.assertFalse(
                modal.query_one("#st-min_return", Input).disabled)
            self.assertFalse(
                modal.query_one("#st-gates").has_class("gate-off"))

    async def test_slot_and_spot_fields_sync(self):
        """static slot_mode greys slot_min; spot_enabled off greys the
        spot posture selects and the bid premium."""
        from textual.widgets import Input, Select
        app = make_app()
        async with app.run_test() as pilot:
            app.push_screen(tui.StrategyModal({
                "slot_mode": "static", "slot_min": 2, "max_rentals": 2,
                "startup_min": 8, "dry_after_min": 15,
                "spot_enabled": False, "spot_probe_mode": "auto",
                "spot_slot_mode": "auto", "spot_bid_premium": 1.01}))
            await pilot.pause()
            modal = app.screen
            self.assertTrue(
                modal.query_one("#st-slot_min", Input).disabled)
            self.assertFalse(
                modal.query_one("#st-max_rentals", Input).disabled)
            for k in ("startup_min", "dry_after_min"):
                self.assertFalse(
                    modal.query_one(f"#st-{k}", Input).disabled, k)
            for k in ("spot_probe_mode", "spot_slot_mode"):
                self.assertTrue(
                    modal.query_one(f"#st-{k}", Select).disabled, k)
            self.assertTrue(
                modal.query_one("#st-spot_bid_premium", Input).disabled)
            modal.query_one("#st-slot_mode", Select).value = "dynamic"
            await pilot.pause()
            self.assertFalse(
                modal.query_one("#st-slot_min", Input).disabled)


if __name__ == "__main__":
    unittest.main()
