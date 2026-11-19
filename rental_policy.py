"""Explicit operator order policy; never creates or cancels an order."""

import hashlib
import re
import uuid
from decimal import Decimal, InvalidOperation, ROUND_CEILING

from market import DEFAULT_COUNTRY_BLOCKLIST, available, cuda_version

BOOTSTRAP_COMMIT = "eb3af58a857cb1906b50515a49692161a34cef7f"
BOOTSTRAP_SHA256 = "01dfe899bc4341f2e0846b637f05a67c675abba9cf2e401f54b63766fcd59cd6"
BOOTSTRAP_URL = (
    "https://raw.githubusercontent.com/dcogswell/clorqub/"
    f"{BOOTSTRAP_COMMIT}/rig/bootstrap.sh"
)
CURRENCY = "USD-Blockchain"
# hard blast-radius ceiling on concurrent slot rentals: the dynamic
# slot scaler grows toward this on share evidence, and pace/budget
# still bound the actual fills
MAX_AUTO_RENTALS = 8
MAX_BURST_PROBES = 6
MAX_HOURS = 24
COST_MARGIN_USD = Decimal("0.01")

# worker aliases carry no server/order id -- the pool dashboard is
# public and a winning rig's box shouldn't be identifiable
WORKER_ANIMALS = [
    "badger", "bear", "bison", "cat", "cobra", "crane", "crow",
    "deer", "eagle", "falcon", "ferret", "finch", "fox", "goat",
    "heron", "ibis", "koala", "lemur", "lynx", "mole", "newt",
    "otter", "owl", "panda", "puma", "raven", "seal", "sloth",
    "stoat", "tiger", "viper", "walrus", "wolf", "wren", "yak",
]


def new_alias(server_id, salt):
    """rig-<animal>-<hex>: the animal is deterministic per server (a
    pubkey-salted hash, so the server_id can't be recovered from it)
    -- the same box shows the same animal on the pool dashboard. The
    suffix stays random so each rental's worker name is unique."""
    digest = hashlib.sha256(f"{server_id}:{salt}".encode()).digest()
    animal = WORKER_ANIMALS[digest[0] % len(WORKER_ANIMALS)]
    return f"rig-{animal}-{uuid.uuid4().hex[:4]}"


def amount(value, label, allow_zero=False):
    try:
        if isinstance(value, bool):
            raise ValueError()
        result = Decimal(str(value))
        if not result.is_finite() or result < 0 or (not allow_zero and result == 0):
            raise ValueError()
        return result
    except (ValueError, InvalidOperation):
        raise ValueError(f"Invalid {label}") from None


def offered_threads(specs):
    """Logical CPUs in Clore's cpus field ("cores/threads"), as used
    for orders."""
    parts = re.findall(r"\d+", str(specs.get("cpus") or ""))
    return int(parts[-1]) if parts else None


def prepare_order(server, fees, *, hours, max_cost_usd, pool, public_key,
                  alias, cpu_threads=None, auto_renew=False,
                  expected_gpu_count=1, mode="on-demand",
                  spot_bid_usd_day=None, spot_floor_usd_day=None):
    """Quote an on-demand order with an explicit whole-hour term.
    auto_renew defaults to False; True asks Clore to re-bill each term.
    expected_gpu_count defaults to 1: only an explicitly approved
    multi-GPU trial may pass 2, and only for whitelisted servers.
    mode="spot" quotes a spot bid instead: no term, no renewal, hourly
    billed per minute at the bid; `hours` is only the budget-accounting
    horizon the cost bound covers."""
    if mode not in ("on-demand", "spot"):
        raise ValueError("mode must be 'on-demand' or 'spot'")
    if isinstance(hours, bool) or not isinstance(hours, int) or hours < 1:
        raise ValueError("Hours must be an integer of at least 1")
    if not isinstance(auto_renew, bool):
        raise ValueError("auto_renew must be a boolean")
    if isinstance(expected_gpu_count, bool) \
            or not isinstance(expected_gpu_count, int) \
            or expected_gpu_count not in (1, 2):
        raise ValueError("expected_gpu_count must be 1 or 2")
    if pool not in ("jetski", "qli"):
        raise ValueError("Pool must be jetski or qli")
    if not re.fullmatch(r"(?:cq|rig)-[A-Za-z0-9-]{1,48}", alias):
        raise ValueError("Invalid worker alias")
    if not public_key.startswith(("ssh-ed25519 ", "ssh-rsa ", "ecdsa-sha2-")) \
            or "\n" in public_key or len(public_key) > 3000:
        raise ValueError("A valid single-line SSH public key is required")
    if not available(server):
        raise ValueError("Server is not currently available")
    specs = server.get("specs") or {}
    country = str((specs.get("net") or {}).get("cc") or "").upper()
    if not country or country in DEFAULT_COUNTRY_BLOCKLIST:
        raise ValueError("Country unknown or blocked")
    if CURRENCY not in (server.get("allowed_coins") or []):
        raise ValueError("Server does not advertise USD-Blockchain")
    gpus = server.get("gpu_array") or []
    cuda = cuda_version(server.get("cuda_version"))
    if len(gpus) != expected_gpu_count or cuda is None \
            or cuda < (12, 0):
        raise ValueError(
            f"First live path requires {expected_gpu_count} GPU(s) "
            "and CUDA >= 12")
    bid = floor = None
    if mode == "spot":
        bid = amount(spot_bid_usd_day, "spot bid USD/day")
        if spot_floor_usd_day is not None:
            floor = amount(spot_floor_usd_day, "spot floor USD/day")
            if bid < floor:
                raise ValueError(
                    "Spot bid is below the server's USD-Blockchain floor")
    else:
        # the term ceiling is whatever the host advertises in `mrl`
        # (Clore hosts offer anywhere from 6h to 3000h); the built-in
        # MAX_HOURS cap only applies when the field is absent
        mrl = server.get("mrl")
        host_max = int(mrl) if isinstance(mrl, (int, float)) \
            and not isinstance(mrl, bool) and mrl >= 1 else None
        limit = host_max if host_max is not None else MAX_HOURS
        if hours > limit:
            src = (f"the host's maximum rental length ({limit}h)"
                   if host_max is not None
                   else f"the maximum rental term ({MAX_HOURS}h)")
            raise ValueError(f"Requested hours exceed {src}")
    price = bid if mode == "spot" else amount(
        ((server.get("price") or {}).get("on_demand") or {}).get(CURRENCY),
        "native USD/day price",
    )
    if not isinstance(fees, dict) or fees.get("code") != 0:
        raise ValueError("Live renter fee response is unavailable")
    fee_key = "spot" if mode == "spot" else "on_demand"
    try:
        renter = fees["renter_fees"][fee_key]
        percent = amount(renter["base"][CURRENCY], "base renter fee", True)
        percent += amount(renter["extra"][CURRENCY], "extra renter fee", True)
        creation = amount(fees["creation_fees"][fee_key][CURRENCY],
                          "creation fee", True)
    except (KeyError, TypeError):
        raise ValueError("Live USD fee schedule is incomplete") from None
    hourly = price * (1 + percent / 100) / 24
    bound = (hourly * hours + creation + COST_MARGIN_USD).quantize(
        Decimal("0.01"), rounding=ROUND_CEILING)
    cap = bound if max_cost_usd is None else amount(max_cost_usd, "maximum order cost")
    if bound > cap:
        raise ValueError(f"Quoted cost bound ${bound} exceeds your ${cap} cap")
    if cpu_threads is None:
        # stock miner convention (amountOfThreads:0 -> nproc-1): one
        # logical CPU stays free to feed the GPU and post shares
        cpu_threads = max(1, (offered_threads(specs) or 2) - 1)
    if isinstance(cpu_threads, bool) or not isinstance(cpu_threads, int) or not 1 <= cpu_threads <= 512:
        raise ValueError("CPU threads must be an integer from 1 to 512")
    bootstrap = (
        f"curl -fsSL --retry 5 {BOOTSTRAP_URL} -o /tmp/bootstrap.sh"
        f' && echo "{BOOTSTRAP_SHA256}  /tmp/bootstrap.sh" | sha256sum -c'
        f" && exec bash /tmp/bootstrap.sh {BOOTSTRAP_COMMIT}"
    )
    payload = {
        "renting_server": int(server["id"]), "type": mode,
        "currency": CURRENCY, "image": "cloreai/hiveos:0.4",
        "ports": {"22": "tcp"},
        "command": "bash -c '" + bootstrap + "'",
        "env": {
            "POOL": pool, "JETSKI_MODE": "pplns", "ALIAS": alias,
            "CPU_THREADS": str(cpu_threads), "GPU": "on",
            "CLORQUB_SSH_PUBKEY": public_key,
        },
    }
    if mode == "spot":
        payload["spotprice"] = float(bid)
    else:
        payload.update(required_price=float(price), rental_hours=hours,
                       auto_renew=auto_renew)
    result = {
        "payload": payload, "cost_bound_usd": str(bound),
        "hourly_cost_usd": str(hourly), "creation_fee_usd": str(creation),
        "max_cost_usd": str(cap), "hours": hours, "alias": alias,
        "auto_renew": auto_renew, "mode": mode,
        "cpu_threads": cpu_threads, "listed_cpu_allocation": specs.get("cpus"),
        "expected_gpu_count": expected_gpu_count,
        "bootstrap_commit": BOOTSTRAP_COMMIT,
    }
    if mode == "spot":
        result["spot_bid_usd_day"] = str(bid)
        result["spot_floor_usd_day"] = \
            str(floor) if floor is not None else None
    return result
