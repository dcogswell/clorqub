"""Persistence and manual-rental policy; never contacts Clore."""
import contextlib
import io
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import blacklist
import rent


NOW = datetime(2026, 11, 15, tzinfo=timezone.utc)


class BlacklistTests(unittest.TestCase):
    def test_expiry_duplicates_removal_and_manual_priority(self):
        with sqlite3.connect(":memory:") as db:
            blacklist.initialize(db)
            first = blacklist.add(db, 1, "flaky_ssh", "auto",
                                  detail={"failed": 30}, now=NOW)
            self.assertIsNotNone(first)
            self.assertIsNone(blacklist.add(db, 1, "fault:spec", "auto", now=NOW))
            self.assertEqual(blacklist.active(db, NOW)[1]["detail"], {"failed": 30})
            later = NOW + timedelta(days=3)
            self.assertEqual(blacklist.active(db, later), {})
            blacklist.add(db, 1, "operator", "manual", now=later)
            blacklist.add(db, 1, "flaky_ssh", "auto", now=later)
            self.assertEqual(blacklist.active(db, later)[1]["source"], "manual")
            self.assertEqual(blacklist.remove(db, 1, later), 2)
            self.assertEqual(blacklist.active(db, later), {})
            # Expired records remain intact for the audit trail.
            self.assertIsNone(db.execute(
                "SELECT removed_at FROM blacklist WHERE id=?", (first,)).fetchone()[0])

    def test_readonly_and_missing_table_and_cli_db_position(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.db")
            db = sqlite3.connect(path)
            self.assertEqual(blacklist.active(db, NOW), {})
            db.close()
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(blacklist.main([
                    "add", "--server", "12", "--reason", "test", "--db", path]), 0)
                self.assertEqual(blacklist.main(["--db", path, "list"]), 0)
                self.assertEqual(blacklist.main(["list", "--all", "--db", path]), 0)
            with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as ro:
                self.assertEqual(blacklist.active(ro)[12]["reason"], "test")
            with contextlib.redirect_stdout(io.StringIO()):
                blacklist.main(["rm", "--server", "12", "--db", path])
            with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as ro:
                self.assertEqual(blacklist.active(ro), {})

    def test_manual_quote_warns_but_returns_review_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.db")
            key = os.path.join(tmp, "key")
            for name, content in ((key, "test"), (key + ".pub", "ssh-ed25519 test")):
                with open(name, "w") as f:
                    f.write(content)
            db = rent._unguarded_db(path)
            blacklist.initialize(db)
            blacklist.add(db, 7, "flaky_ssh", "manual")
            db.close()
            with mock.patch.object(rent, "load_secrets", return_value=("test", {})), \
                    mock.patch.object(rent, "marketplace",
                                      return_value={"servers": [{"id": 7}]}), \
                    mock.patch.object(rent, "fees", return_value={}), \
                    mock.patch.object(rent, "my_orders", return_value=[]), \
                    mock.patch.object(rent.rental_policy, "prepare_order",
                                      return_value={}), \
                    mock.patch.object(rent, "create_order_flow") as create:
                warnings = []
                ctx = rent.quote_order(
                    server_id=7, hours=1, max_cost_usd="1", pool="jetski",
                    cpu_threads=None, db_path=path, env_file="/unused",
                    key_path=key, warn=warnings.append)
                self.assertEqual(ctx["blacklist"]["reason"], "flaky_ssh")
                self.assertTrue(any("manual rental allowed" in w for w in warnings))
                create.assert_not_called()
