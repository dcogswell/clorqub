"""Tests for the read-only scan CLI. No real API calls: fetch_marketplace
is always mocked."""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import cli  # noqa: E402


def listing(server_id=1, gpu="RTX 3090 Ti", cpu="AMD Ryzen 9 7950X",
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
            "gpuram": 24,
            "ram": 64,
            "net": {"cc": country},
        },
    }


def snapshot_file(servers):
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump({"code": 0, "servers": servers}, f)
    return path


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), \
            contextlib.redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


class TestScanSnapshot(unittest.TestCase):
    def test_valid_snapshot_ranks_and_does_not_create_memory_file(self):
        path = snapshot_file([listing()])
        try:
            code, out, err = run(
                ["scan", "--snapshot", path, "--db", ":memory:"])
        finally:
            os.unlink(path)
        self.assertEqual(code, 0, err)
        self.assertFalse(os.path.exists(":memory:"))
        self.assertIn("snapshot", out)
        self.assertIn("RTX 3090 Ti", out)  # full Ti name, not truncated
        self.assertIn(">350", out)         # lower-bound marker
        self.assertIn("7950X", out)
        self.assertIn("provisional", out)
        self.assertIn("not a rental approval", out)

    def test_missing_snapshot_is_clean_error(self):
        code, out, err = run(
            ["scan", "--snapshot", "/nonexistent/x.json",
             "--db", ":memory:"])
        self.assertNotEqual(code, 0)
        self.assertIn("error:", err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(out, "")

    def test_malformed_snapshot_is_clean_error(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            f.write("{ not json")
        try:
            code, out, err = run(
                ["scan", "--snapshot", path, "--db", ":memory:"])
        finally:
            os.unlink(path)
        self.assertNotEqual(code, 0)
        self.assertIn("error:", err)
        self.assertNotIn("Traceback", err)

    def test_db_dir_permission_error_is_clean(self):
        path = snapshot_file([listing()])
        try:
            with mock.patch.object(cli.os, "makedirs",
                                   side_effect=PermissionError("denied")):
                code, out, err = run(
                    ["scan", "--snapshot", path,
                     "--db", "/tmp/x/clorqub.sqlite3"])
        finally:
            os.unlink(path)
        self.assertNotEqual(code, 0)
        self.assertIn("error:", err)
        self.assertNotIn("Traceback", err)


class TestScanLive(unittest.TestCase):
    def test_env_key_wins_without_reading_env_file(self):
        payload = {"code": 0, "servers": [listing()]}
        with mock.patch.dict(cli.os.environ,
                             {"CLORE_API_KEY": "ENVKEY"}), \
                mock.patch.object(cli, "load_env_file") as env_file, \
                mock.patch.object(cli, "fetch_marketplace",
                                  return_value=payload) as fetch:
            code, out, err = run(["scan", "--live", "--db", ":memory:"])
        self.assertEqual(code, 0, err)
        env_file.assert_not_called()
        fetch.assert_called_once_with("ENVKEY")

    def test_fetch_failure_never_leaks_key(self):
        sentinel = "SENTINEL-API-KEY-12345"
        with mock.patch.dict(cli.os.environ,
                             {"CLORE_API_KEY": sentinel}), \
                mock.patch.object(
                    cli, "fetch_marketplace",
                    side_effect=RuntimeError(
                        f"bad header line: auth: {sentinel}")):
            code, out, err = run(["scan", "--live", "--db", ":memory:"])
        self.assertNotEqual(code, 0)
        blob = out + err
        self.assertNotIn(sentinel, blob)
        self.assertIn("RuntimeError", err)
        self.assertNotIn("Traceback", err)

    def test_missing_key_is_clean_error(self):
        env = {k: v for k, v in os.environ.items()
               if k != "CLORE_API_KEY"}
        with mock.patch.dict(cli.os.environ, env, clear=True), \
                mock.patch.object(cli, "load_env_file",
                                  return_value={}):
            code, out, err = run(["scan", "--live", "--db", ":memory:"])
        self.assertNotEqual(code, 0)
        self.assertIn("CLORE_API_KEY", err)
        self.assertNotIn("Traceback", err)


if __name__ == "__main__":
    unittest.main()
