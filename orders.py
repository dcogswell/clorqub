"""Read-only discovery of Clore orders, including externally created rentals."""

import json
import math
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from market import utcnow

REQUEST_GAP_SECONDS = 1.1
SCHEMA = """
CREATE TABLE IF NOT EXISTS order_inventory (
    id INTEGER PRIMARY KEY,
    captured_at TEXT NOT NULL,
    ok INTEGER NOT NULL,
    orders_json TEXT,
    error_kind TEXT
);
CREATE TABLE IF NOT EXISTS wallet_snapshots (
    id INTEGER PRIMARY KEY,
    captured_at TEXT NOT NULL,
    ok INTEGER NOT NULL,
    wallets_json TEXT,
    error_kind TEXT
);
"""


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) and value >= 0 else None
    except (ValueError, TypeError):
        return None


def sanitize_orders(payload):
    """Whitelist financial/hardware metadata; never retain credentials or commands."""
    if not isinstance(payload, dict) or payload.get("code") != 0:
        raise ValueError("unsuccessful order inventory")
    if not isinstance(payload.get("orders"), list):
        raise ValueError("invalid order inventory")
    result, seen = [], set()
    for raw in payload["orders"]:
        if (not isinstance(raw, dict) or isinstance(raw.get("id"), bool)
                or not isinstance(raw.get("id"), int)):
            raise ValueError("invalid order")
        order_id = int(raw["id"])
        if order_id <= 0 or order_id in seen:
            raise ValueError("invalid or duplicated order id")
        seen.add(order_id)
        server_id = _number(raw.get("si"))
        item = {
            "order_id": order_id,
            "server_id": int(server_id) if server_id is not None and server_id > 0 and server_id.is_integer() else None,
            "currency": raw.get("currency") if isinstance(raw.get("currency"), str) else None,
            "price_day_native": _number(raw.get("price")),
            "spend_reported_native": _number(raw.get("spend")),
            "quoted_creation_fee_native": _number(raw.get("creation_fee")),
            "created_unix": _number(raw.get("ct")),
            "rental_end": _number(raw.get("rental_end")),
            "term_end": _number(raw.get("term_end")),
            "rental_hours": _number(raw.get("rental_hours")),
            "auto_renew": raw.get("auto_renew")
            if isinstance(raw.get("auto_renew"), bool) else None,
            "online_reported": raw.get("online") if isinstance(raw.get("online"), bool) else None,
            "spot": raw.get("spot") if isinstance(raw.get("spot"), bool) else None,
            "control": "observe-only",
        }
        specs = raw.get("specs") or {}
        item["cpu"] = specs.get("cpu") if isinstance(specs, dict) and isinstance(specs.get("cpu"), str) else None
        gpus = raw.get("gpu_array")
        item["gpus"] = [gpu for gpu in gpus if isinstance(gpu, str)] if isinstance(gpus, list) else []
        result.append(item)
    return result


def fetch_inventory(api_key):
    request = Request(
        "https://api.clore.ai/v1/my_orders",
        headers={"auth": api_key, "Accept": "application/json", "User-Agent": "clorqub/0.1"},
    )
    with urlopen(request, timeout=25) as response:
        return json.load(response)


def fetch_wallets(api_key):
    request = Request(
        "https://api.clore.ai/v1/wallets",
        headers={"auth": api_key, "Accept": "application/json",
                 "User-Agent": "clorqub/0.1"},
    )
    with urlopen(request, timeout=25) as response:
        return json.load(response)


def sanitize_wallets(payload):
    """Whitelist currency name + balance; never retain addresses/ids."""
    if not isinstance(payload, dict) or payload.get("code") != 0:
        raise ValueError("unsuccessful wallet fetch")
    wallets = payload.get("wallets")
    if not isinstance(wallets, list):
        raise ValueError("invalid wallet payload")
    result = []
    for raw in wallets:
        if not isinstance(raw, dict) or not isinstance(
                raw.get("name"), str):
            raise ValueError("invalid wallet entry")
        result.append({"name": raw["name"],
                       "balance": _number(raw.get("balance"))})
    return result


def refresh_wallets(db, api_key, stop_event=None):
    """One bounded /v1/wallets call per scan cycle; best effort."""
    if stop_event is not None:
        if stop_event.wait(REQUEST_GAP_SECONDS):
            return False
    else:
        time.sleep(REQUEST_GAP_SECONDS)
    db.executescript(SCHEMA)
    items, error = None, None
    try:
        items = sanitize_wallets(fetch_wallets(api_key))
    except HTTPError as exc:
        error = f"HTTP {exc.code}"
    except (URLError, OSError, ValueError, KeyError, TypeError,
            OverflowError):
        error = "Wallet request or schema failed"
    with db:
        db.execute(
            "INSERT INTO wallet_snapshots(captured_at, ok, wallets_json,"
            " error_kind) VALUES (?, ?, ?, ?)",
            (utcnow(), int(error is None),
             json.dumps(items, sort_keys=True)
             if items is not None else None, error),
        )
    return error is None


def latest_wallets(db):
    if not db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND"
            " name='wallet_snapshots'").fetchone():
        return {"wallets": [], "captured_at": None}
    row = db.execute(
        "SELECT captured_at, wallets_json FROM wallet_snapshots "
        "WHERE ok=1 ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        return {"wallets": [], "captured_at": None}
    try:
        wallets = json.loads(row["wallets_json"] or "[]")
    except (ValueError, TypeError):
        wallets = []
    return {"wallets": wallets if isinstance(wallets, list) else [],
            "captured_at": row["captured_at"]}


def refresh_inventory(db, api_key, stop_event=None):
    """One spaced request per live scan; failed requests never become an empty fleet."""
    if stop_event is not None:
        if stop_event.wait(REQUEST_GAP_SECONDS):
            return False
    else:
        time.sleep(REQUEST_GAP_SECONDS)
    db.executescript(SCHEMA)
    items, error = None, None
    try:
        items = sanitize_orders(fetch_inventory(api_key))
    except HTTPError as exc:
        error = f"HTTP {exc.code}"
    except (URLError, OSError, ValueError, KeyError, TypeError, OverflowError):
        error = "Order inventory request or schema failed"
    with db:
        db.execute(
            "INSERT INTO order_inventory(captured_at, ok, orders_json, error_kind) "
            "VALUES (?, ?, ?, ?)",
            (utcnow(), int(error is None),
             json.dumps(items, sort_keys=True) if items is not None else None, error),
        )
    return error is None


def read_inventory(db):
    result = {
        "orders": [], "captured_at": None, "last_attempt_at": None,
        "ok": False, "error": "Order inventory has not been collected.",
        "revision": None,
    }
    if not db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='order_inventory'"
    ).fetchone():
        return result
    attempt = db.execute("SELECT * FROM order_inventory ORDER BY id DESC LIMIT 1").fetchone()
    if attempt is None:
        return result
    good = db.execute(
        "SELECT captured_at, orders_json FROM order_inventory WHERE ok=1 ORDER BY id DESC LIMIT 1"
    ).fetchone()
    result.update(
        revision=attempt["id"], last_attempt_at=attempt["captured_at"],
        ok=bool(attempt["ok"]), error=attempt["error_kind"],
    )
    if good is not None:
        result.update(orders=json.loads(good["orders_json"]), captured_at=good["captured_at"])
    return result
