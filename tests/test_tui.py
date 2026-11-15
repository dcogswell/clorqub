"""Tests for the TUI rent-flow progress view: Hide only hides the
log -- the rental worker must keep running, must not be cancelled by
the periodic reload, and `b` must reopen the live log."""

import asyncio
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


if __name__ == "__main__":
    unittest.main()
