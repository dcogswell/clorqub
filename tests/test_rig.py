"""Unit tests for the clorqub rig agent. Stdlib only:
python3 -m unittest discover -s tests
"""

import hashlib
import json
import os
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "rig"))

import agent  # noqa: E402
import qubic_parse as qp  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "fixtures")


class TestRenderConfig(unittest.TestCase):
    def test_jetski_pplns(self):
        cfg = agent.render_jetski_config(
            "pplns", "WALLET1", "rig-1", 7, False)
        self.assertEqual(cfg["pool"]["url"],
                         "wss://pplns.jtskxpool.ai/ws")
        self.assertTrue(cfg["pool"]["pps"])
        self.assertEqual(cfg["pool"]["wallet"], "WALLET1")
        self.assertEqual(cfg["pool"]["alias"], "rig-1")
        self.assertEqual(cfg["miner"]["cpu"]["threads"], 7)
        self.assertTrue(cfg["miner"]["cpu"]["enabled"])
        self.assertFalse(cfg["miner"]["gpu"]["enabled"])
        self.assertEqual(cfg["api"]["bind"], "127.0.0.1:17899")

    def test_jetski_solo(self):
        cfg = agent.render_jetski_config(
            "solo", "WALLET1", "rig-2", 3, True)
        self.assertEqual(cfg["pool"]["url"],
                         "wss://solo.jtskxpool.ai/ws")
        self.assertFalse(cfg["pool"]["pps"])
        self.assertTrue(cfg["miner"]["gpu"]["enabled"])

    def test_qli(self):
        cfg = agent.render_qli_config("TOKEN1", "rig-3", 5, True)
        cs = cfg["ClientSettings"]
        self.assertEqual(cs["accessToken"], "TOKEN1")
        self.assertEqual(cs["alias"], "rig-3")
        self.assertEqual(cs["poolAddress"], "wss://wps.qubic.li/ws")
        self.assertEqual(cs["trainer"]["cpuThreads"], 5)
        self.assertTrue(cs["trainer"]["cpu"])
        self.assertTrue(cs["trainer"]["gpu"])


class TestPackageVerification(unittest.TestCase):
    def _make_tarball(self, tmpdir):
        """Build a small tarball + its .hash sibling inside tmpdir."""
        src = os.path.join(tmpdir, "src", "qubminer")
        os.makedirs(src)
        with open(os.path.join(src, "h-manifest.conf"), "w") as f:
            f.write("CUSTOM_VERSION=9.9.9\n")
        tar_path = os.path.join(tmpdir, "pkg.tar.gz")
        with tarfile.open(tar_path, "w:gz") as tf:
            tf.add(src, arcname="qubminer")
        with open(tar_path, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
        with open(os.path.join(tmpdir, "pkg.hash"), "w") as f:
            f.write(digest + "\n")
        return tar_path, digest

    def test_fetch_expected_hash_and_sha256(self):
        with tempfile.TemporaryDirectory() as td:
            tar_path, digest = self._make_tarball(td)
            self.assertEqual(agent.sha256_file(tar_path), digest)
            url = "file://" + tar_path
            self.assertEqual(agent.fetch_expected_hash(url), digest)

    def test_fetch_package_accepts_match(self):
        with tempfile.TemporaryDirectory() as td:
            tar_path, digest = self._make_tarball(td)
            url = "file://" + tar_path
            dest = os.path.join(td, "out")
            info = agent.fetch_package(url, dest)
            self.assertEqual(info["sha256"], digest)
            self.assertEqual(info["version"], "9.9.9")
            self.assertTrue(os.path.isdir(info["dir"]))
            # second call reuses the extracted dir
            info2 = agent.fetch_package(url, dest)
            self.assertTrue(info2["cached"])

    def test_fetch_package_rejects_mismatch(self):
        with tempfile.TemporaryDirectory() as td:
            tar_path, _ = self._make_tarball(td)
            with open(os.path.join(td, "pkg.hash"), "w") as f:
                f.write("0" * 64 + "\n")
            with self.assertRaises(ValueError):
                agent.fetch_package("file://" + tar_path,
                                    os.path.join(td, "out"))


class TestManifest(unittest.TestCase):
    def test_qubminer_version(self):
        manifest = os.path.join(FIXTURES, "h-manifest-qubminer.conf")
        d = tempfile.mkdtemp()
        import shutil
        shutil.copy(manifest, os.path.join(d, "h-manifest.conf"))
        self.assertEqual(agent.read_manifest_version(d), "3.13.6")

    def test_qubjetski_version(self):
        manifest = os.path.join(FIXTURES, "h-manifest-qubjetski.conf")
        d = tempfile.mkdtemp()
        import shutil
        shutil.copy(manifest, os.path.join(d, "h-manifest.conf"))
        self.assertEqual(agent.read_manifest_version(d), "4.5")


class TestLogParsing(unittest.TestCase):
    def test_jetski_status_line(self):
        line = ("2026-11-14 12:00:00.000 QUBJETSKI E:189 | SHARES: 3/3 "
                "(R:0) | [AVX2] 1234 it/s | 1200 avg it/s")
        m = qp.STATUS_RE.search(line)
        self.assertIsNotNone(m)
        self.assertEqual(m.group("epoch"), "189")
        self.assertEqual(m.group("found"), "3")
        self.assertEqual(m.group("submitted"), "3")
        self.assertEqual(m.group("it"), "1234")
        self.assertEqual(m.group("avg"), "1200")

    def test_qli_status_line(self):
        line = ("2026-11-14 12:00:00.123 [INFO]  E:236 | SHARES: 94/94 "
                "(R:17) | 100 it/s | 84 avg it/s")
        m = qp.STATUS_RE.search(line)
        self.assertIsNotNone(m)
        self.assertEqual(m.group("epoch"), "236")
        self.assertEqual(m.group("rejected"), "17")
        self.assertEqual(m.group("it"), "100")

    def test_trainer_regexes(self):
        m = qp.TRAINER_UPDATED_RE.search(
            "Trainer Updated: AVX2    v236.17")
        self.assertIsNotNone(m)
        self.assertEqual(m.group("variant"), "AVX2")
        m = qp.TRAINER_START_RE.search(
            "[CPU] Trainer: pplns cpu 236.3 is starting")
        self.assertIsNotNone(m)
        self.assertEqual(m.group("flavor"), "pplns")
        self.assertEqual(m.group("ver"), "236.3")
        m = qp.TRAINER_VARIANT_RE.search("[CPU] Trainer: Version: AVX2")
        self.assertIsNotNone(m)
        self.assertEqual(m.group("variant"), "AVX2")


class TestParseIts(unittest.TestCase):
    def test_jetski_combined_line(self):
        its = agent.parse_its(os.path.join(FIXTURES, "jetski_cpu_gpu.log"))
        self.assertEqual(its["cpu"], 1)
        self.assertEqual(its["gpu"], 84)
        self.assertEqual(its["total"], 85)
        self.assertEqual(its["avg"], 78)
        self.assertEqual(its["epoch"], 239)
        self.assertEqual(its["devices"], {"AVX2": 1, "CUDA": 84})

    def test_qli_alternating_lines(self):
        its = agent.parse_its(os.path.join(FIXTURES, "qli_cpu_gpu.log"))
        self.assertEqual(its["cpu"], 0)
        self.assertEqual(its["gpu"], 41)
        self.assertEqual(its["total"], 41)
        self.assertEqual(its["avg"], 19)
        self.assertEqual(its["devices"], {"CUDA": 41, "AVX2": 0})

    def test_cpu_only(self):
        with tempfile.NamedTemporaryFile(
                "w", suffix=".log", delete=False) as f:
            f.write("2026-11-14T16:00:00.000+00:00 E:1 | SHARES: 0/0 "
                    "(R:0) | [AVX2] 7 it/s | 6 avg it/s\n")
            path = f.name
        its = agent.parse_its(path)
        os.unlink(path)
        self.assertEqual(its["cpu"], 7)
        self.assertIsNone(its["gpu"])
        self.assertEqual(its["total"], 7)

    def test_empty(self):
        its = agent.parse_its("/nonexistent.log")
        self.assertIsNone(its["total"])
        self.assertEqual(its["devices"], {})


class TestPoolState(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self._base = agent.BASE
        self._spath = agent.STATE_PATH
        agent.BASE = self.td.name
        agent.STATE_PATH = os.path.join(self.td.name, "state.json")

    def tearDown(self):
        agent.BASE = self._base
        agent.STATE_PATH = self._spath

    def test_state_overrides_env(self):
        agent.save_state({"pool": "qli", "updated_at": "x", "boot_count": 3})
        pool, source = agent.resolve_pool(agent.load_state(), "jetski")
        self.assertEqual((pool, source), ("qli", "state"))

    def test_missing_state_falls_back_to_env(self):
        pool, source = agent.resolve_pool(agent.load_state(), "qli")
        self.assertEqual((pool, source), ("qli", "env"))

    def test_invalid_state_falls_back_to_env(self):
        agent.save_state({"pool": "bogus"})
        pool, source = agent.resolve_pool(agent.load_state(), "jetski")
        self.assertEqual((pool, source), ("jetski", "env"))

    def test_invalid_env_pool_defaults_jetski(self):
        pool, source = agent.resolve_pool({}, "bogus")
        self.assertEqual((pool, source), ("jetski", "env"))

    def test_switch_persists_pool(self):
        agent.save_state({"pool": "jetski", "boot_count": 1})
        st = agent.load_state()
        st["pool"] = "qli"  # what the switch path does
        agent.save_state(st)
        self.assertEqual(agent.load_state()["pool"], "qli")
        pool, _ = agent.resolve_pool(agent.load_state(), "jetski")
        self.assertEqual(pool, "qli")


class TestNoCredentialLeak(unittest.TestCase):
    def test_status_sanitized(self):
        secrets = ["SUPERSECRET_WALLET", "SUPERSECRET_TOKEN"]
        status = {
            "pool": "jetski",
            "miner": {"pid": 123},
            "nested": {"note": "connected as SUPERSECRET_WALLET ok"},
            "list": ["fine", "xSUPERSECRET_TOKENx"],
            "jetski_summary": {"wallet": "SUPERSECRET_WALLET"},
        }
        out = agent.sanitize_status(status, secrets)
        blob = json.dumps(out)
        self.assertNotIn("SUPERSECRET_WALLET", blob)
        self.assertNotIn("SUPERSECRET_TOKEN", blob)
        self.assertEqual(out["miner"]["pid"], 123)

    def test_written_status_never_contains_credentials(self):
        creds = {"jetski_wallet": "WAL-abc-123",
                 "qli_access_token": "TOK-xyz-789"}
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(agent, "STATUS_PATH",
                                   os.path.join(td, "status.json")):
                status = {"pool": "qli", "shares": {"found": 1},
                          "alias": "clorqub-test"}
                agent.write_status(status, agent.secrets_of(creds))
                blob = open(agent.STATUS_PATH).read()
        self.assertNotIn("WAL-abc-123", blob)
        self.assertNotIn("TOK-xyz-789", blob)


if __name__ == "__main__":
    unittest.main()
