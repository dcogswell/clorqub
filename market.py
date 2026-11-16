"""Read-only Clore marketplace scanner and conservative yield scorer."""

import hashlib
import json
import math
import re
import sqlite3
import urllib.request
from datetime import datetime, timezone

API_URL = "https://api.clore.ai/v1/marketplace"
DEFAULT_COUNTRY_BLOCKLIST = {"RU", "UA", "BY"}
DEFAULT_MIN_CUDA = (12, 0)
RENTER_FEES = {"on-demand": 0.05, "spot": 0.0125}

# Relative Qubic GPU it/s multiplier vs RTX 4090 = 1.0. Absolute it/s
# swings with each epoch's difficulty, but the ratios between models
# transfer. Anchors: pool.qubic.li hashrate table (RTX 5090 ~55 vs
# RTX 4090 ~25 Mit/s -> 2.2x) and the advanced-hash.ai Qubic calculator
# (4090/4080S/4070Ti/3090/3070/3060Ti/3060). Unlisted models are
# interpolated from relative compute benchmarks -- rough priors only.
GPU_IT_SCALE = {
    "RTX 5090": 2.20,
    "RTX 5080": 0.80,
    "RTX 5070 Ti": 0.65,
    "RTX 5070": 0.50,
    "RTX 5060 Ti": 0.30,
    "RTX 5060": 0.22,
    "RTX 4090": 1.00,
    "RTX 4080": 0.65,
    "RTX 4070 Ti": 0.48,
    "RTX 4070": 0.40,
    "RTX 4060 Ti": 0.30,
    "RTX 4060": 0.22,
    "RTX 3090 Ti": 0.48,
    "RTX 3090": 0.45,
    "RTX 3080 Ti": 0.42,
    "RTX 3080": 0.38,
    "RTX 3070 Ti": 0.31,
    "RTX 3070": 0.28,
    "RTX 3060 Ti": 0.25,
    "RTX 3060": 0.19,
    "RTX 3050": 0.15,
    "RTX 2080 Ti": 0.30,
    "RTX 2080": 0.25,
    "RTX 2070": 0.20,
    "RTX 2060": 0.15,
}

# These are end-to-end jetski observations supplied by the user for a
# high-end Zen 4 Ryzen CPU. They are not GPU-only rates.
USER_BENCHMARKS = (
    {
        "gpu_model": "RTX 3070",
        "cpu_profile": "ryzen9_7945hx_or_7950x",
        "cpu_isa": "AVX512",
        "pool": "jetski",
        "observation_kind": "approximate",
        "min_sols_hr": 160.0,
        "max_sols_hr": None,
        "source": "user_observation",
        "notes": "Approximate end-to-end rig rate; CPU thread allocation unspecified.",
    },
    {
        "gpu_model": "RTX 3080 Ti",
        "cpu_profile": "ryzen9_7945hx_or_7950x",
        "cpu_isa": "AVX512",
        "pool": "jetski",
        "observation_kind": "lower_bound",
        "min_sols_hr": 350.0,
        "max_sols_hr": None,
        "source": "user_observation",
        "notes": "User reports well over 350 sols/hr: a source lower bound, not a candidate guarantee.",
    },
    {
        "gpu_model": "RTX 3090 Ti",
        "cpu_profile": "ryzen9_7945hx_or_7950x",
        "cpu_isa": "AVX512",
        "pool": "jetski",
        "observation_kind": "lower_bound",
        "min_sols_hr": 350.0,
        "max_sols_hr": None,
        "source": "user_observation",
        "notes": "User reports well over 350 sols/hr: a source lower bound, not a candidate guarantee.",
    },
)

# Context only: these observations must not drive a cross-trainer conversion
# or imply CPU/GPU solution attribution that the user did not provide.
USER_RATE_CONTEXT = (
    {
        "id": "user-cpu-rate",
        "device": "CPU",
        "gpu_model": None,
        "its": 35,
        "qualifier": "approximate",
        "notes": "CPU rate from the related rental discussion; exact run unspecified.",
    },
    {
        "id": "user-unspecified-gpu-rate",
        "device": "GPU",
        "gpu_model": None,
        "its": 230,
        "qualifier": "approximate",
        "notes": "GPU rate from the discussion; exact GPU/run not explicitly identified.",
    },
    {
        "id": "user-3070-gpu-rate",
        "device": "GPU",
        "gpu_model": "RTX 3070",
        "its": 100,
        "qualifier": "tentative",
        "notes": "User recalls about 100 it/s for RTX 3070 ('I think').",
    },
)

REFERENCE_UNCERTAINTY = (
    "Reference CPU allocation, measurement duration, epoch and trainer version "
    "are unknown; yield is provisional, not thread-normalized or spend-approved."
)


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_env_file(path):
    """Load KEY=VALUE entries without evaluating shell syntax."""
    values = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key, value = key.strip(), value.strip()
                if key.startswith("export "):
                    key = key[7:].strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                    value = value[1:-1]
                values[key] = value
    except FileNotFoundError:
        pass
    return values


def fetch_marketplace(api_key):
    request = urllib.request.Request(
        API_URL, headers={
            "auth": api_key,
            "Accept": "application/json",
            "User-Agent": "clorqub/0.1",
        }
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)
    if not isinstance(payload, dict) or payload.get("code") not in (0, None):
        code = payload.get("code") if isinstance(payload, dict) else "invalid"
        raise RuntimeError(f"Clore marketplace request failed (code {code})")
    servers = payload.get("servers")
    if not isinstance(servers, list):
        raise RuntimeError("Clore marketplace response has no servers list")
    return payload


def cuda_version(value):
    match = re.match(r"\s*(\d+)(?:\.(\d+))?", str(value or ""))
    return (int(match.group(1)), int(match.group(2) or 0)) if match else None


def normalize_gpu(value):
    text = str(value or "").upper()
    if re.search(r"\b(?:LAPTOP|MOBILE)\b", text):
        return None
    match = re.search(r"\bRTX\s*(\d{4})\s*(TI)?\b", text)
    if not match:
        return None
    return f"RTX {match.group(1)}" + (" Ti" if match.group(2) else "")


def cpu_profile(cpu):
    """Infer ISA conservatively; only exact reference CPU models get a prior."""
    text = str(cpu or "").upper().replace("(R)", "").replace("(TM)", "")
    if "UNKNOWN" in text or not text.strip():
        return "unknown", "unknown"
    if re.search(r"\bRYZEN\s+9\s+(?:7945HX|7950X)\b", text):
        return "ryzen9_7945hx_or_7950x", "AVX512_inferred"
    if re.search(r"\bRYZEN\s+9\s+(?:7900X3D|7950X3D|7900X?|9900X|9950X(?:3D)?)\b", text):
        return "avx512_unbenchmarked", "AVX512_inferred"
    if re.search(r"\bEPYC\s+(?:9\d{2}[45]|4\d{2}[45])[FP]?\b", text):
        return "avx512_unbenchmarked", "AVX512_inferred"
    if re.search(r"\bTHREADRIPPER(?:\s+PRO)?\s+7\d{3}", text):
        return "avx512_unbenchmarked", "AVX512_inferred"
    if re.search(r"\bXEON\s+(?:GOLD|SILVER|PLATINUM|BRONZE)\s+\d{4}", text):
        return "avx512_unbenchmarked", "AVX512_inferred"
    if re.search(r"\bRYZEN\s+[3579]\s+[1-6]\d{3}[A-Z0-9]*\b", text):
        return "avx2_unbenchmarked", "AVX2_inferred"
    return "avx2_or_unknown", "AVX2_or_unknown"


def cuda_capable(server):
    """QUBIC GPU mining is CUDA-only; Clore reports cuda_version only
    for NVIDIA machines, so the field itself is the capability test."""
    if not isinstance(server.get("gpu_array"), list) \
            or not server["gpu_array"]:
        return False
    parsed = cuda_version(server.get("cuda_version"))
    return parsed is not None and parsed >= DEFAULT_MIN_CUDA


def offered_gpu(server):
    """Return the single advertised GPU model; avoid extrapolating multi-GPU."""
    gpu_array = server.get("gpu_array")
    if not isinstance(gpu_array, list) or len(gpu_array) != 1:
        return None
    return normalize_gpu(gpu_array[0])


def available(server):
    if server.get("rented") is not False:
        return False
    partial = server.get("partial_gpu_rental")
    if isinstance(partial, dict):
        try:
            if int(partial.get("available_gpus", 0)) < 1:
                return False
        except (TypeError, ValueError):
            return False
    return True


def price_per_day_usd(server, mode):
    price = server.get("price") or {}
    if mode == "on-demand":
        allowed = server.get("allowed_coins")
        if allowed is not None and "USD-Blockchain" not in allowed:
            return None
        value = (price.get("usd") or {}).get("on_demand_usd")
        if isinstance(value, bool):
            return None
        try:
            value = float(value)
        except (TypeError, ValueError):
            return None
        return value if math.isfinite(value) and value > 0 else None
    # Clore's marketplace has no USD spot quote. Never treat its documented
    # zero placeholder as a free rental.
    return None


def benchmark_index(benchmarks=None):
    rows = USER_BENCHMARKS if benchmarks is None else benchmarks
    return {(b["gpu_model"], b["cpu_profile"]): b for b in rows
            if b.get("pool") == "jetski"}


def score_listing(server, mode="on-demand", benchmarks=None):
    specs = server.get("specs") or {}
    country = ((specs.get("net") or {}).get("cc") or "").upper()
    profile, isa = cpu_profile(specs.get("cpu"))
    gpu = offered_gpu(server)
    price_day = price_per_day_usd(server, mode)
    cuda = cuda_version(server.get("cuda_version"))
    index = benchmark_index(benchmarks)
    benchmark = index.get((gpu, profile))

    if not available(server):
        reason = "unavailable"
    elif country in DEFAULT_COUNTRY_BLOCKLIST:
        reason = "blocked country"
    elif cuda is None or cuda < DEFAULT_MIN_CUDA:
        reason = "CUDA unknown or below 12.0"
    elif gpu is None:
        reason = "not a single-GPU listing"
    elif price_day is None or price_day <= 0:
        reason = "no supported USD price"
    else:
        reason = None

    fee = RENTER_FEES[mode]
    hourly_cost = (price_day / 24.0) * (1 + fee) if price_day else None
    sols_hr = benchmark["min_sols_hr"] if benchmark else None
    return {
        "server_id": server.get("id"),
        "owner": server.get("owner"),
        "country": country,
        "gpu": gpu,
        "cpu": specs.get("cpu") or "Unknown CPU",
        "cpu_profile": profile,
        "cpu_isa": isa,
        "threads_listed": specs.get("cpus"),
        "cuda": server.get("cuda_version"),
        "reliability": server.get("reliability"),
        "price_day_usd": price_day,
        "effective_price_day_usd": price_day * (1 + fee) if price_day else None,
        "hourly_cost_usd": hourly_cost,
        "estimated_sols_hr": sols_hr,
        "estimated_sols_per_usd": (
            sols_hr / hourly_cost if sols_hr is not None and hourly_cost else None
        ),
        "benchmarked": benchmark is not None,
        "benchmark_source": benchmark["source"] if benchmark else None,
        "benchmark_notes": benchmark["notes"] if benchmark else None,
        "benchmark_kind": benchmark["observation_kind"] if benchmark else None,
        "pool": "jetski",
        "reference_uncertainty": REFERENCE_UNCERTAINTY,
        "spend_approved": False,
        "eligible": reason is None,
        "ineligible_reason": reason,
    }


SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS marketplace_snapshots (
    id INTEGER PRIMARY KEY,
    captured_at TEXT NOT NULL,
    source TEXT NOT NULL,
    server_count INTEGER NOT NULL,
    raw_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS servers (
    server_id INTEGER PRIMARY KEY,
    owner INTEGER,
    country TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    latest_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS server_specs (
    id INTEGER PRIMARY KEY,
    server_id INTEGER NOT NULL REFERENCES servers(server_id),
    spec_hash TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    valid_to TEXT,
    specs_json TEXT NOT NULL,
    cpu_model TEXT,
    cpu_offer TEXT,
    gpu_array_json TEXT NOT NULL,
    gpu_count INTEGER NOT NULL,
    vram_gb REAL,
    cuda_version TEXT,
    ram_gb REAL,
    net_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS server_prices (
    id INTEGER PRIMARY KEY,
    server_id INTEGER NOT NULL REFERENCES servers(server_id),
    observed_at TEXT NOT NULL,
    valid_to TEXT,
    on_demand_usd_day REAL,
    spot_usd_day REAL,
    price_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS performance_benchmarks (
    id INTEGER PRIMARY KEY,
    gpu_model TEXT NOT NULL,
    cpu_profile TEXT NOT NULL,
    cpu_isa TEXT NOT NULL,
    pool TEXT NOT NULL,
    observation_kind TEXT NOT NULL,
    min_sols_hr REAL NOT NULL,
    max_sols_hr REAL,
    source TEXT NOT NULL,
    thread_count INTEGER,
    epoch INTEGER,
    trainer_version TEXT,
    notes TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(gpu_model, cpu_profile, pool, source)
);
CREATE TABLE IF NOT EXISTS performance_observations (
    observation_id TEXT PRIMARY KEY,
    raw_json TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS active_server_spec
    ON server_specs(server_id) WHERE valid_to IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS active_server_price
    ON server_prices(server_id) WHERE valid_to IS NULL;
"""


def connect_db(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA)
    return db


def validate_marketplace(payload):
    if not isinstance(payload, dict) or payload.get("code") not in (0, None):
        raise ValueError("marketplace payload is not a successful response")
    servers = payload.get("servers")
    if not isinstance(servers, list):
        raise ValueError("marketplace payload must contain a servers list")
    seen = set()
    for server in servers:
        if not isinstance(server, dict):
            raise ValueError("marketplace contains an invalid server listing")
        try:
            server_id = int(server["id"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("marketplace listing has an invalid server id") from None
        if server_id <= 0 or server_id in seen:
            raise ValueError("marketplace contains an invalid or duplicate server id")
        seen.add(server_id)
        for key in ("specs", "price"):
            if not isinstance(server.get(key), dict):
                raise ValueError(f"marketplace listing has invalid {key}")
        if not isinstance(server.get("gpu_array"), list):
            raise ValueError("marketplace listing has an invalid gpu_array")
    return servers


_DISK_FREE = re.compile(r"\s*\d+(?:\.\d+)?GB\s*$")
VOLATILE_SPEC_KEYS = ("pl",)
FX_USD_KEYS = ("on_demand_clore", "on_demand_btc")


def spec_change_key(spec_doc):
    """sha256 of the spec doc minus live readings (free disk, power draw)."""
    specs = dict(spec_doc.get("specs") or {})
    for key in VOLATILE_SPEC_KEYS:
        specs.pop(key, None)
    if isinstance(specs.get("disk"), str):
        specs["disk"] = _DISK_FREE.sub("", specs["disk"])
    doc = {**spec_doc, "specs": specs}
    return hashlib.sha256(
        json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def price_change_key(price):
    """Owner-set price only: drop the FX-converted CLORE/BTC amounts."""
    p = json.loads(json.dumps(price or {}))
    if isinstance(p.get("usd"), dict):
        for key in FX_USD_KEYS:
            p["usd"].pop(key, None)
    for mode in ("on_demand", "spot"):
        if isinstance(p.get(mode), dict):
            p[mode] = {k: v for k, v in p[mode].items() if k == "USD-Blockchain"}
    return json.dumps(p, sort_keys=True)


def ingest_marketplace(db, payload, source="api", captured_at=None):
    validate_marketplace(payload)
    # An import is atomic: a malformed listing must not leave partial history.
    with db:
        _ingest_marketplace(db, payload, source, captured_at)


def _ingest_marketplace(db, payload, source, captured_at):
    captured_at = captured_at or utcnow()
    servers = payload["servers"]
    cursor = db.execute(
        "INSERT INTO marketplace_snapshots(captured_at, source, server_count, raw_json) "
        "VALUES (?, ?, ?, ?)",
        (captured_at, source, len(servers), json.dumps(payload, sort_keys=True)),
    )
    # Only the newest snapshot keeps the multi-MB payload; older rows
    # keep their timestamps and counts.
    db.execute(
        "UPDATE marketplace_snapshots SET raw_json='' WHERE id<? AND raw_json!=''",
        (cursor.lastrowid,),
    )
    for server in servers:
        server_id = int(server["id"])
        specs = server.get("specs") or {}
        country = ((specs.get("net") or {}).get("cc") or "").upper()
        encoded = json.dumps(server, sort_keys=True)
        db.execute(
            """INSERT INTO servers(server_id, owner, country, first_seen, last_seen, latest_json)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(server_id) DO UPDATE SET owner=excluded.owner,
                 country=excluded.country, last_seen=excluded.last_seen,
                 latest_json=excluded.latest_json""",
            (server_id, server.get("owner"), country, captured_at, captured_at, encoded),
        )
        gpu_array = server.get("gpu_array") or []
        spec_doc = {
            "specs": specs, "gpu_array": gpu_array,
            "cuda_version": server.get("cuda_version"),
        }
        spec_json = json.dumps(spec_doc, sort_keys=True, separators=(",", ":"))
        spec_hash = spec_change_key(spec_doc)
        previous = db.execute(
            "SELECT id, specs_json FROM server_specs WHERE server_id=? AND valid_to IS NULL",
            (server_id,),
        ).fetchone()
        prev_key = (
            spec_change_key(json.loads(previous["specs_json"]))
            if previous is not None else None
        )
        if previous is None or prev_key != spec_hash:
            if previous is not None:
                db.execute("UPDATE server_specs SET valid_to=? WHERE id=?",
                           (captured_at, previous["id"]))
            db.execute(
                """INSERT INTO server_specs
                   (server_id, spec_hash, observed_at, specs_json, cpu_model, cpu_offer,
                    gpu_array_json, gpu_count, vram_gb, cuda_version, ram_gb, net_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    server_id, spec_hash, captured_at, spec_json, specs.get("cpu"),
                    specs.get("cpus"), json.dumps(gpu_array), len(gpu_array),
                    specs.get("gpuram"), server.get("cuda_version"), specs.get("ram"),
                    json.dumps(specs.get("net") or {}, sort_keys=True),
                ),
            )
        price_json = json.dumps(server["price"], sort_keys=True)
        previous = db.execute(
            "SELECT id, price_json FROM server_prices WHERE server_id=? AND valid_to IS NULL",
            (server_id,),
        ).fetchone()
        prev_price_key = (
            price_change_key(json.loads(previous["price_json"]))
            if previous is not None else None
        )
        if previous is None or prev_price_key != price_change_key(server["price"]):
            if previous is not None:
                db.execute("UPDATE server_prices SET valid_to=? WHERE id=?",
                           (captured_at, previous["id"]))
            db.execute(
                """INSERT INTO server_prices
                   (server_id, observed_at, on_demand_usd_day, spot_usd_day, price_json)
                   VALUES (?, ?, ?, NULL, ?)""",
                (server_id, captured_at, price_per_day_usd(server, "on-demand"), price_json),
            )
    seed_benchmarks(db)


def seed_benchmarks(db):
    now = utcnow()
    for row in USER_BENCHMARKS:
        db.execute(
            """INSERT INTO performance_benchmarks
               (gpu_model, cpu_profile, cpu_isa, pool, observation_kind, min_sols_hr, max_sols_hr,
                source, thread_count, notes, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
               ON CONFLICT(gpu_model, cpu_profile, pool, source) DO UPDATE SET
                 cpu_isa=excluded.cpu_isa, min_sols_hr=excluded.min_sols_hr,
                 max_sols_hr=excluded.max_sols_hr, notes=excluded.notes,
                 observation_kind=excluded.observation_kind,
                 updated_at=excluded.updated_at""",
            (
                row["gpu_model"], row["cpu_profile"], row["cpu_isa"],
                row["pool"], row["observation_kind"],
                row["min_sols_hr"], row["max_sols_hr"], row["source"],
                row["notes"], now,
            ),
        )
    for observation in USER_RATE_CONTEXT:
        db.execute(
            """INSERT INTO performance_observations(observation_id, raw_json)
               VALUES (?, ?) ON CONFLICT(observation_id) DO UPDATE SET
               raw_json=excluded.raw_json""",
            (observation["id"], json.dumps(observation, sort_keys=True)),
        )
