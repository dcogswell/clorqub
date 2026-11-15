"""Conservative presentation of agent measurements, without yield conversions."""

import math
from datetime import datetime, timezone

POLL_SECONDS = 30.0
STALE_SECONDS = 120
MAX_CLOCK_AHEAD_SECONDS = 60


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("Missing agent timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Agent timestamp needs a timezone")
    return parsed


def number(value, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    if integer:
        return int(value) if value == int(value) else None
    return value


def text(value, limit=200):
    return value[:limit] if isinstance(value, str) else None


def sanitize_status(raw, expected_alias=None):
    """Retain only known non-secret metrics; preserve unknowns instead of zeroing."""
    if not isinstance(raw, dict) or raw.get("pool") not in ("jetski", "qli"):
        raise ValueError("Invalid agent status")
    timestamp(raw.get("updated_at"))
    alias = raw.get("alias")
    if not isinstance(alias, str) or not alias:
        raise ValueError("Missing worker alias")
    if expected_alias is not None and alias != expected_alias:
        raise ValueError("Worker identity mismatch")
    miner = raw.get("miner") or {}
    rates = raw.get("its") or {}
    shares = raw.get("shares") or {}
    trainer = raw.get("trainer") or {}
    package = raw.get("package") or {}
    hardware = raw.get("hardware") or {}
    if not all(isinstance(part, dict) for part in
               (miner, rates, shares, trainer, package, hardware)):
        raise ValueError("Invalid metric shape")
    devices = rates.get("devices") or {}
    if not isinstance(devices, dict):
        devices = {}
    return {
        "pool": raw["pool"], "alias": text(alias),
        "updated_at": raw["updated_at"],
        "miner": {
            "running": miner.get("running") if isinstance(miner.get("running"), bool) else None,
            "started_at": text(miner.get("started_at")),
            "restarts": number(miner.get("restarts"), integer=True),
        },
        "its": {
            "cpu": number(rates.get("cpu")), "gpu": number(rates.get("gpu")),
            "devices": {name: number(devices.get(name)) for name in
                        ("AVX2", "AVX512", "CUDA", "CUDA12")},
        },
        "shares": {name: number(shares.get(name), integer=True) for name in
                   ("epoch", "found", "submitted", "invalid",
                    "log_found", "log_submitted", "log_rejected")},
        "trainer": {name: text(trainer.get(name)) for name in ("variant", "flavor", "version")},
        "package": {name: text(package.get(name)) for name in ("version", "sha256")},
        "hardware": {
            "cpu_model": text(hardware.get("cpu_model"), 400),
            "nproc": number(hardware.get("nproc"), integer=True),
            "avx2": hardware.get("avx2") if isinstance(hardware.get("avx2"), bool) else None,
            "avx512f": hardware.get("avx512f") if isinstance(hardware.get("avx512f"), bool) else None,
            "gpus": [text(gpu, 500) for gpu in hardware.get("gpus", [])[:16]
                     if isinstance(gpu, str)] if isinstance(hardware.get("gpus"), list) else None,
        },
    }


def health(row, now=None):
    if row.get("provision_state") == "cancelled":
        return "cancelled"
    if not row.get("ssh_host") or not row.get("ssh_port"):
        return "awaiting SSH"
    if row.get("error_kind"):
        return "unreachable" if row["error_kind"] in ("SSHError", "OSError") else "telemetry error"
    status = row.get("status")
    if not status or not row.get("observed_at"):
        return "awaiting telemetry"
    now = now or datetime.now(timezone.utc)
    try:
        captured_age = (now - timestamp(row["observed_at"])).total_seconds()
        source_age = (now - timestamp(status["updated_at"])).total_seconds()
    except (TypeError, ValueError, KeyError):
        return "unknown"
    if source_age < -MAX_CLOCK_AHEAD_SECONDS or captured_age < -MAX_CLOCK_AHEAD_SECONDS:
        return "clock skew"
    if captured_age > STALE_SECONDS or source_age > STALE_SECONDS:
        return "stale"
    running = (status.get("miner") or {}).get("running")
    if running is False:
        return "miner stopped"
    if running is not True:
        return "unknown"
    rates = status.get("its") or {}
    if any(isinstance(rates.get(key), (int, float)) and rates[key] > 0
           for key in ("cpu", "gpu")):
        return "mining"
    return "running/no rate"
