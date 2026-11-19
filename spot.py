"""Bounded, opt-in spot-floor observations. No bids or rental operations."""

import json
import math
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from market import RENTER_FEES, score_listing, utcnow

DEFAULT_SPOT_LIMIT = 0
MAX_SPOT_LIMIT = 10
REQUEST_GAP_SECONDS = 1.1
QUOTE_MAX_AGE_SECONDS = 900

SCHEMA = """
CREATE TABLE IF NOT EXISTS spot_quotes (
    id INTEGER PRIMARY KEY,
    server_id INTEGER NOT NULL REFERENCES servers(server_id),
    captured_at TEXT NOT NULL,
    raw_json TEXT,
    error_kind TEXT
);
CREATE INDEX IF NOT EXISTS spot_quote_server ON spot_quotes(server_id, id);
"""


def latest_quotes(db):
    """Compatible with scanner databases created before spot support."""
    if not db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='spot_quotes'"
    ).fetchone():
        return {}
    return {
        row["server_id"]: dict(row) for row in db.execute(
            "SELECT q.* FROM spot_quotes q JOIN "
            "(SELECT server_id, MAX(id) AS id FROM spot_quotes GROUP BY server_id) "
            "latest ON q.id=latest.id"
        )
    }


def summarize_quote(record, now=None):
    result = {
        "captured_at": None, "status": "not collected",
        "floor_usd_day": None, "fee_inclusive_floor_usd_day": None,
        "offers_count": None,
        "note": "Opt in with --spot-limit 3; ranking still uses on-demand prices.",
    }
    if not record:
        return result
    result["captured_at"] = record["captured_at"]
    try:
        captured = datetime.fromisoformat(record["captured_at"].replace("Z", "+00:00"))
        age = ((now or datetime.now(timezone.utc)) - captured).total_seconds()
        if age < 0 or age > QUOTE_MAX_AGE_SECONDS:
            result.update(status="stale", note="Spot observation is older than 15 minutes.")
            return result
        if record.get("error_kind"):
            result.update(status="fetch failed", note=record["error_kind"])
            return result
        payload = json.loads(record["raw_json"])
        if payload.get("code") != 0 or payload.get("exists") is not True:
            result.update(status="no market", note="No successful spot-market response.")
            return result
        market = payload["market"]
        server = market["server"]
        if server.get("online") is not True or server.get("visibility") != "public":
            result.update(status="unavailable", note="Spot server is offline or not public.")
            return result
        offers = market.get("offers")
        value = server["min_pricing"].get("USD-Blockchain")
        if not isinstance(offers, list) or isinstance(value, bool):
            raise ValueError("unknown quote shape")
        value = float(value)
        if not math.isfinite(value) or value <= 0:
            raise ValueError("invalid USD floor")
        result.update(
            status="offers present" if offers else "floor only",
            floor_usd_day=value,
            fee_inclusive_floor_usd_day=value * (1 + RENTER_FEES["spot"]),
            offers_count=len(offers),
            note=(
                "Competing offers reported; winning bid and offer currencies are not inferred."
                if offers else
                "No offers reported at observation time; floor is not an executable bid guarantee."
            ),
        )
    except (ValueError, TypeError, KeyError, AttributeError):
        result.update(status="unsupported", note="No verified USD-Blockchain spot floor.")
    return result


def bid_for(record, now=None, premium=1.01):
    """The bid a fresh spot order would place, or None when the cached
    quote is unusable (same rules as summarize_quote). The bid is the
    USD floor, or `premium` x the top competing USD-Blockchain bid when
    offers exist (Clore requires at least 1.01x), rounded up to the
    cent."""
    summary = summarize_quote(record, now)
    floor = summary["floor_usd_day"]
    if floor is None:
        return None
    offers = json.loads(record["raw_json"])["market"].get("offers") or []
    usd = [o for o in offers if isinstance(o, dict)
           and o.get("currency") == "USD-Blockchain"]
    # a standing (inactive) offer is still a queued bid, and Clore
    # requires 1% over the top bid in the currency regardless of which
    # one currently holds the server; only our own offer is not
    # competition
    competing = [float(o["bid"]) for o in usd
                 if o.get("my") is not True
                 and not isinstance(o.get("bid"), bool)
                 and isinstance(o.get("bid"), (int, float))
                 and math.isfinite(o["bid"]) and o["bid"] > 0]
    top = max(competing) if competing else None
    bid = floor if top is None else max(floor, top * premium)
    bid = math.ceil(round(bid * 100, 9)) / 100
    return {"floor_usd_day": floor, "top_bid_usd_day": top,
            "offers": len(usd), "bid_usd_day": bid}


def fetch_quote(api_key, server_id):
    request = Request(
        f"https://api.clore.ai/v1/spot_marketplace?market={int(server_id)}",
        headers={"auth": api_key, "Accept": "application/json", "User-Agent": "clorqub/0.1"},
    )
    with urlopen(request, timeout=25) as response:
        return json.load(response)


def refresh_quotes(db, servers, api_key, limit, stop_event=None):
    """At most limit requests, spread out; query only benchmark-matched candidates."""
    if limit == 0:
        return 0
    if not isinstance(limit, int) or not 0 <= limit <= MAX_SPOT_LIMIT:
        raise ValueError("spot limit must be an integer from 0 to 10")
    db.executescript(SCHEMA)
    previous = latest_quotes(db)
    candidates = [score_listing(server) for server in servers]
    candidates = sorted(
        (row for row in candidates
         if row["eligible"] and row["estimated_sols_per_usd"] is not None),
        key=lambda row: row["estimated_sols_per_usd"], reverse=True,
    )
    # Unseen/oldest observations go first; preserve value order for ties.
    candidates.sort(
        key=lambda row: previous.get(row["server_id"], {}).get("captured_at", "")
    )
    return _fetch_quotes_for(
        db, (row["server_id"] for row in candidates[:limit]),
        api_key, stop_event)


def _fetch_quotes_for(db, server_ids, api_key, stop_event=None):
    """One spaced request per server id; record every attempt."""
    attempts = 0
    for server_id in server_ids:
        if stop_event is not None:
            if stop_event.wait(REQUEST_GAP_SECONDS):
                break
        else:
            time.sleep(REQUEST_GAP_SECONDS)
        payload, error, abort = None, None, False
        try:
            payload = fetch_quote(api_key, server_id)
            abort = not isinstance(payload, dict) or payload.get("code") != 0
        except HTTPError as exc:
            error = f"HTTP {exc.code}"
            abort = exc.code in (401, 403, 429)
        except (URLError, OSError, ValueError):
            error = "Network or JSON response error"
            abort = True
        with db:
            db.execute(
                "INSERT INTO spot_quotes(server_id, captured_at, raw_json, error_kind) "
                "VALUES (?, ?, ?, ?)",
                (int(server_id), utcnow(),
                 json.dumps(payload, sort_keys=True) if payload is not None else None, error),
            )
        attempts += 1
        if abort:
            break
    return attempts


def refresh_quotes_for(db, server_ids, api_key, stop_event=None):
    """Targeted spot-market refresh for an explicit server list (e.g.
    the active spot rentals); same spacing as refresh_quotes."""
    db.executescript(SCHEMA)
    return _fetch_quotes_for(db, list(dict.fromkeys(
        int(sid) for sid in server_ids)), api_key, stop_event)
