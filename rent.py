#!/usr/bin/env python3
"""Operator rental CLI: create (dry-run default), list, cancel, enroll,
reconcile. On-demand USD-Blockchain only, <=24h, unbounded manual
orders (the MAX_AUTO_RENTALS cap is for a future auto-selector),
renewal off unless --auto-renew, no spot, no auto-retries on POSTs."""

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import blacklist
import fleet_store
import rental_policy
import rig_ssh
import telemetry_policy
from cli import DEFAULT_DB, DEFAULT_ENV
from market import connect_db, fetch_marketplace, load_env_file

API = "https://api.clore.ai/v1"
GAP_SECONDS = 1.1
CREATE_DELAY_SECONDS = 5.0
RECONCILE_TRIES = 3
RECONCILE_GAP_SECONDS = 5.0
PROVISION_SECONDS = 180.0
PROVISION_GAP_SECONDS = 8.0
DEFAULT_KEY = "~/.config/clorqub/id_ed25519"
HOST_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}")
PORT_RE = re.compile(r"^22:(\d{1,5})$")


class APIError(Exception):
    pass


def err(msg):
    print(f"error: {msg}", file=sys.stderr)


def _get(path, api_key):
    time.sleep(GAP_SECONDS)
    req = Request(f"{API}/{path}", headers={
        "auth": api_key, "Accept": "application/json",
        "User-Agent": "clorqub/0.1"})
    try:
        with urlopen(req, timeout=25) as r:
            return json.load(r)
    except HTTPError as exc:
        raise APIError(f"HTTP {exc.code}") from None
    except (URLError, OSError, ValueError):
        raise APIError("request failed") from None


def _post_once(path, api_key, body):
    """Exactly one POST; never retried by the caller on any outcome."""
    time.sleep(GAP_SECONDS)
    try:
        req = Request(
            f"{API}/{path}", data=json.dumps(body).encode(),
            headers={"auth": api_key, "Accept": "application/json",
                     "User-Agent": "clorqub/0.1",
                     "Content-Type": "application/json"})
        with urlopen(req, timeout=30) as r:
            payload = json.load(r)
        return payload, None
    except HTTPError as exc:
        return None, f"HTTP {exc.code}"
    except (URLError, OSError, ValueError):
        return None, "request failed"


def my_orders(api_key):
    """Raw inventory (not sanitized): needs si/id/ct/rental_hours/
    auto_renew/pub_cluster/tcp_ports. Fails closed on ANY malformed
    entry so manual orders are never under-counted."""
    payload = _get("my_orders", api_key)
    if not isinstance(payload, dict) or payload.get("code") != 0 \
            or not isinstance(payload.get("orders"), list):
        raise APIError("order inventory unavailable")
    items, seen = [], set()
    for entry in payload["orders"]:
        if not isinstance(entry, dict):
            raise APIError("malformed order entry")
        for field in ("id", "si"):
            value = entry.get(field)
            if isinstance(value, bool) or not isinstance(value, int) \
                    or value <= 0:
                raise APIError("malformed order entry")
        if entry["id"] in seen:
            raise APIError("duplicate order id")
        seen.add(entry["id"])
        items.append(entry)
    return items


def fees(api_key):
    return _get("renter_fees", api_key)


def marketplace(api_key):
    time.sleep(GAP_SECONDS)
    try:
        return fetch_marketplace(api_key)
    except Exception:
        # never surface raw request/HTTP errors that could echo the key
        raise APIError("marketplace request failed") from None


def _first(iterable):
    return iterable if isinstance(iterable, list) else \
        ([iterable] if isinstance(iterable, str) else [])


def endpoint(order):
    """SSH endpoint from a raw order. pub_cluster and tcp_ports are
    lists in the real API (single strings tolerated); first valid
    hostname + '22:PORT' mapping (1..65535), fixed root user."""
    host = None
    for cand in _first(order.get("pub_cluster")):
        if isinstance(cand, str) and HOST_RE.fullmatch(cand) \
                and ".." not in cand:
            host = cand
            break
    port = None
    for cand in _first(order.get("tcp_ports")):
        if isinstance(cand, str):
            m = PORT_RE.match(cand.strip())
            if m:
                p = int(m.group(1))
                if 1 <= p <= 65535:
                    port = p
                    break
    return host, port


def load_secrets(env_file, need_api=True, need_creds=False):
    """Process env first, then the protected env file; never returned
    values are printed."""
    out = dict(os.environ)
    try:
        file_env = load_env_file(os.path.expanduser(env_file))
    except OSError:
        file_env = {}
    merged = {**file_env, **{k: v for k, v in out.items() if v}}
    api_key = merged.get("CLORE_API_KEY") if need_api else None
    if need_api and not api_key:
        raise ValueError("CLORE_API_KEY missing (env or env file)")
    creds = None
    if need_creds:
        wallet = merged.get("JETSKI_WALLET")
        token = merged.get("QLI_ACCESS_TOKEN")
        if not wallet or not token:
            raise ValueError(
                "JETSKI_WALLET and QLI_ACCESS_TOKEN are both required")
        creds = {"jetski_wallet": wallet, "qli_access_token": token}
    return api_key, creds


def verify_bootstrap():
    """The pinned bootstrap URL must hash to the pinned sha256 before
    any paid POST can use it."""
    req = Request(rental_policy.BOOTSTRAP_URL,
                  headers={"User-Agent": "clorqub/0.1"})
    try:
        with urlopen(req, timeout=25) as r:
            data = r.read()
    except (URLError, OSError):
        raise APIError("bootstrap download failed") from None
    if hashlib.sha256(data).hexdigest() != rental_policy.BOOTSTRAP_SHA256:
        raise APIError("pinned bootstrap sha256 mismatch")


def open_operator_db(path, live):
    db_path = os.path.expanduser(path)
    if db_path == ":memory:":
        if live:
            raise ValueError("live actions require a real --db path")
        db = connect_db(db_path)
        fleet_store.initialize(db)
        return db, None
    os.makedirs(os.path.dirname(os.path.abspath(db_path)),
                exist_ok=True)
    db = connect_db(db_path)
    fleet_store.initialize(db)
    lock_file = open(db_path + ".operator.lock", "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        db.close()
        lock_file.close()
        raise ValueError(
            "another rent.py process holds the operator lock")
    return db, lock_file


def creation_unix(order):
    """ct is the order creation unix timestamp."""
    ct = order.get("ct")
    try:
        return datetime.fromtimestamp(float(ct), timezone.utc).timestamp()
    except (TypeError, ValueError, OSError):
        return None


def find_new_orders(items, before_ids, server_id):
    return [o for o in items
            if o.get("id") not in before_ids and o.get("si") == server_id]


def reconcile_new_order(api_key, before_ids, server_id):
    """Poll my_orders until exactly one new order on this server is
    seen or tries run out. Returns (order|None, count)."""
    found = []
    for attempt in range(RECONCILE_TRIES):
        if attempt:
            time.sleep(RECONCILE_GAP_SECONDS)
        try:
            found = find_new_orders(my_orders(api_key), before_ids,
                                    server_id)
        except APIError:
            found = []
        if len(found) == 1:
            return found[0], 1
    return None, len(found)


def _terms_signature(server, quote):
    """Everything the operator confirmed; ANY change must abort."""
    specs = server.get("specs") or {}
    return (
        quote["cost_bound_usd"], quote["hourly_cost_usd"],
        quote["creation_fee_usd"], quote["listed_cpu_allocation"],
        quote["payload"].get("required_price"),
        specs.get("cpu"), specs.get("cpus"), specs.get("ram"),
        json.dumps(server.get("gpu_array"), sort_keys=True),
        str(server.get("cuda_version")),
    )


def _register_managed(db, attempt_id, order, server_id, alias, quote,
                      key_path, warn=err):
    """Idempotent: an existing record for this order finishes the
    attempt when alias/server match; never overwrites a conflicting
    record or duplicates the INSERT."""
    order_id = order["id"]
    existing = db.execute(
        "SELECT * FROM fleet_rentals WHERE order_id=?",
        (order_id,)).fetchone()
    if existing is not None:
        if existing["alias"] == alias \
                and existing["server_id"] == server_id:
            fleet_store.finish_attempt(db, attempt_id, "matched",
                                       order_id)
            return True
        warn(f"order {order_id} already recorded under a different "
             "rental; refusing to overwrite")
        return False
    cts = creation_unix(order)
    deadline_at = (datetime.fromtimestamp(
        cts + quote["hours"] * 3600, timezone.utc
    ).isoformat(timespec="seconds") if cts is not None else None)
    fleet_store.register_rental(
        db, order_id=order_id, server_id=server_id, alias=alias,
        pool=quote.get("pool") or "unknown", deadline_at=deadline_at,
        max_cost_usd=quote.get("max_cost_usd"),
        quoted_cost_usd=quote.get("cost_bound_usd"),
        ssh_key_path=quote.get("ssh_key_path") or key_path,
        auto_renew=quote.get("auto_renew"))
    fleet_store.finish_attempt(db, attempt_id, "matched", order_id)
    return True


def _term_matches(order, hours, want_renew=False):
    # Clore omits auto_renew on orders created without renewal. With
    # want_renew False, explicit null/true/non-boolean values remain unsafe;
    # with True the receipt must echo auto_renew is True (a per-term flag --
    # if Clore ignored our request this fails closed into the auto-cancel
    # path). A missing/different native end is unsafe either way.
    if isinstance(hours, bool) or not isinstance(hours, int) or hours <= 0:
        return False
    requested = order.get("rental_hours")
    if isinstance(requested, bool) or not isinstance(requested, int) or requested != hours:
        return False
    if want_renew is True:
        if order.get("auto_renew") is not True:
            return False
    elif "auto_renew" in order and order["auto_renew"] is not False:
        return False
    if "spot" in order and order["spot"] is not False:
        return False
    created, end = order.get("ct"), order.get("rental_end")
    if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0
           for value in (created, end)):
        return False
    return end == created + hours * 3600


def blacklist_warning(entry):
    return (f"blacklisted: {entry['reason']} ({entry['source']}, "
            f"expires {entry['expires_at'] or 'never'}); manual rental allowed")


def print_quote_summary(server, quote, pool, key_path, blacklist_entry=None):
    specs = server.get("specs") or {}
    renew = quote.get("auto_renew") is True
    print(f"ORDER REVIEW (on-demand, USD-Blockchain, "
          f"{'AUTO-RENEW ON' if renew else 'no renewal'})")
    if blacklist_entry:
        print(f"  WARNING: {blacklist_warning(blacklist_entry)}")
    print(f"  server      {server.get('id')}  "
          f"gpu={server.get('gpu_array')}  cuda={server.get('cuda_version')}")
    print(f"  cpu         {specs.get('cpu')}  listed={quote['listed_cpu_allocation']}  "
          f"threads={quote['cpu_threads']}")
    print(f"  pool        {pool}   alias {quote['alias']}")
    if renew:
        print(f"  hours       {quote['hours']} per term; auto-renew ON -- "
              f"re-bills ~${quote['cost_bound_usd']} every "
              f"{quote['hours']}h term until cancelled (bound covers "
              "ONE term only)")
    else:
        print(f"  hours       {quote['hours']}  (native rental_hours; "
              "the order auto-expires, renewal off)")
    print(f"  cost bound  ${quote['cost_bound_usd']} incl. live fees "
          f"(hourly ${quote['hourly_cost_usd']}, "
          f"creation ${quote['creation_fee_usd']}) "
          f"vs cap ${quote['max_cost_usd']}")
    print(f"  ssh key     {key_path}")
    print("  terms: auto-cancel if the receipt lacks the bounded term, "
          "or if provisioning fails")
    print("  risk: the host can technically read rig memory; credentials "
          "are pushed to the rig")


def _unguarded_db(db_path):
    """connect_db + fleet_store.initialize WITHOUT the operator flock.
    Quote-phase read access only (pending_attempts); live actions still
    go through open_operator_db."""
    path = os.path.expanduser(db_path)
    if path != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(path)),
                    exist_ok=True)
    db = connect_db(path)
    fleet_store.initialize(db)
    return db


def quote_order(*, server_id, hours, max_cost_usd, pool, cpu_threads,
                db_path, env_file, key_path, auto_renew=False,
                say=print, warn=err):
    """Phase 1 of `create`: everything up to the confirmation prompt,
    minus the operator lock. Returns a secret-free ctx dict for
    create_order_flow. Failures are reported via warn() and raised as
    ValueError (validation/policy, safe message) or APIError."""
    def reject(msg):
        warn(msg)
        raise ValueError(msg)

    if isinstance(hours, bool) or not isinstance(hours, int) \
            or not 1 <= hours <= rental_policy.MAX_HOURS:
        reject("hours must be an integer 1.."
               f"{rental_policy.MAX_HOURS}")
    if not isinstance(auto_renew, bool):
        reject("auto_renew must be a boolean")
    key_path = os.path.expanduser(key_path)
    pub_path = key_path + ".pub"
    if not os.path.isfile(key_path) or not os.path.isfile(pub_path):
        reject(f"ssh key pair not found: {key_path}[.pub]; "
               "create a dedicated clorqub key first")
    with open(pub_path) as f:
        public_key = f.read().strip()
    try:
        db = _unguarded_db(db_path)
    except Exception as exc:
        reject(str(exc))
    try:
        blocked = blacklist.active(db).get(server_id)
        if blocked:
            warn(f"server {server_id} {blacklist_warning(blocked)}")
        try:
            api_key, _ = load_secrets(env_file, need_creds=False)
        except ValueError as exc:
            reject(str(exc))
        try:
            payload = marketplace(api_key)
            fee_data = fees(api_key)
            current = my_orders(api_key)
        except APIError as exc:
            warn(f"fresh API data unavailable: {exc}")
            raise APIError(f"fresh API data unavailable: {exc}") \
                from None
        if any(o.get("si") == server_id for o in current):
            reject(f"server {server_id} already has an order")
        pending = fleet_store.pending_attempts(db)
        if pending:
            reject(f"unresolved attempt(s) "
                   f"{[p['attempt_id'] for p in pending] }; "
                   "run `python3 rent.py reconcile` first -- new spend is "
                   "blocked until attempts resolve")
        server = next((s for s in payload.get("servers", [])
                       if s.get("id") == server_id), None)
        if server is None:
            reject(f"server {server_id} not in marketplace")
        alias = rental_policy.new_alias(server_id, public_key)
        try:
            quote = rental_policy.prepare_order(
                server, fee_data, hours=hours,
                max_cost_usd=max_cost_usd, pool=pool,
                public_key=public_key, alias=alias,
                cpu_threads=cpu_threads, auto_renew=auto_renew)
        except ValueError as exc:
            reject(f"policy rejected: {exc}")
        quote["pool"] = pool
        quote["ssh_key_path"] = key_path
        # ctx carries NO secrets: no api_key, no wallet/token creds.
        return {"server_id": server_id, "hours": hours,
                "auto_renew": auto_renew,
                "max_cost_usd": max_cost_usd, "pool": pool,
                "cpu_threads": cpu_threads, "alias": alias,
                "quote": quote, "server": server,
                "public_key": public_key, "key_path": key_path,
                "blacklist": blocked,
                "db_path": db_path, "env_file": env_file}
    finally:
        db.close()


def create_order_flow(ctx, *, say=print, warn=err):
    """Phase 2 of `create`: everything after confirmation, verbatim from
    the old cmd_create tail. Re-opens the operator db under the
    exclusive flock, re-fetches/re-verifies all terms, then makes ONE
    create_order POST (never retried). Returns (rc, info)."""
    info = {"order_id": None, "provision_state": None,
            "stage": "confirm"}
    server_id = ctx["server_id"]
    hours = ctx["hours"]
    pool = ctx["pool"]
    alias = ctx["alias"]
    quote = ctx["quote"]
    key_path = ctx["key_path"]
    try:
        db, lock = open_operator_db(ctx["db_path"], live=True)
    except Exception as exc:
        warn(str(exc))
        info["stage"] = "lock"
        return 2, info
    try:
        info["stage"] = "secrets"
        try:
            api_key, creds = load_secrets(
                ctx["env_file"], need_creds=True)
        except ValueError as exc:
            warn(str(exc))
            return 2, info

        # re-verify fresh terms; nothing may have changed silently
        info["stage"] = "refetch"
        try:
            payload2 = marketplace(api_key)
            fee_data2 = fees(api_key)
            current2 = my_orders(api_key)
        except APIError as exc:
            warn(f"re-check failed: {exc}; no order placed")
            return 1, info
        info["stage"] = "guards"
        if any(o.get("si") == server_id for o in current2):
            warn("server already has an order; aborting")
            return 2, info
        server2 = next((s for s in payload2.get("servers", [])
                        if s.get("id") == server_id), None)
        if server2 is None:
            warn("server disappeared; aborting")
            return 2, info
        try:
            quote2 = rental_policy.prepare_order(
                server2, fee_data2, hours=hours,
                max_cost_usd=ctx["max_cost_usd"], pool=pool,
                public_key=ctx["public_key"], alias=alias,
                cpu_threads=ctx["cpu_threads"],
                auto_renew=ctx["auto_renew"])
        except ValueError as exc:
            warn(f"terms changed since review ({exc}); aborting")
            return 2, info
        if _terms_signature(server2, quote2) != \
                _terms_signature(ctx["server"], quote):
            warn("price/hardware terms changed since review; aborting "
                 "-- re-run for a fresh quote")
            return 2, info

        info["stage"] = "attempt"
        attempt_id = uuid.uuid4().hex
        # use the freshest pre-POST id set so a manual order created
        # in between can't collide with our candidate detection
        before_ids = {o["id"] for o in current2}
        fleet_store.begin_attempt(db, attempt_id, server_id, alias,
                                  sorted(before_ids), quote)
        try:
            verify_bootstrap()
        except APIError as exc:
            fleet_store.finish_attempt(db, attempt_id, "failed")
            warn(f"bootstrap verification failed: {exc}")
            return 1, info

        info["stage"] = "post"
        say(f"attempt {attempt_id}: placing ONE create_order POST "
            "(no retries)...")
        time.sleep(CREATE_DELAY_SECONDS)
        resp, post_err = _post_once("create_order", api_key,
                                    quote["payload"])
        post_ok = (post_err is None and isinstance(resp, dict)
                   and resp.get("code") == 0)
        if post_err:
            say(f"POST failed: {post_err} -- response may still have "
                "created an order; reconciling, DO NOT rerun")
        elif post_ok:
            say("POST accepted; reconciling order id")
        else:
            say("POST returned a failure code -- reconciling anyway; "
                "DO NOT rerun")

        info["stage"] = "reconcile"
        order, count = reconcile_new_order(api_key, before_ids,
                                           server_id)
        if order is None:
            fleet_store.finish_attempt(db, attempt_id, "unknown")
            warn(f"order outcome AMBIGUOUS (matches={count}) after POST. "
                 f"Attempt {attempt_id} stays 'unknown' for safety. "
                 "Check the Clore web UI / `rent.py reconcile`. "
                 "NO automatic retry was made.")
            return 1, info
        if not post_ok:
            fleet_store.finish_attempt(db, attempt_id, "unknown")
            warn(f"candidate order {order['id']} found on server "
                 f"{server_id}, but the POST did not confirm success "
                 "-- it may be a manual order. It was NOT auto-owned, "
                 "cancelled or provisioned. Check the web UI, then run "
                 f"`python3 rent.py reconcile --adopt-order "
                 f"{order['id']}` to claim it or cancel it there.")
            return 1, info
        order_id = order["id"]
        info["order_id"] = order_id
        say(f"order id: {order_id}")

        # bounded-term receipt check BEFORE any provisioning
        info["stage"] = "register"
        term_ok = _term_matches(order, hours, ctx.get("auto_renew"))
        if not _register_managed(db, attempt_id, order, server_id,
                                 alias, quote, key_path, warn=warn):
            return 2, info
        if not term_ok:
            say(f"order {order_id} receipt lacks the bounded term "
                f"(rental_hours={order.get('rental_hours')}, "
                f"rental_end={order.get('rental_end')}, "
                f"auto_renew={order.get('auto_renew', 'omitted/disabled')}); "
                "auto-cancelling that exact order as authorized")
            cresp, cerr = _post_once("cancel_order", api_key,
                                     {"id": int(order_id)})
            if cerr or not (isinstance(cresp, dict)
                            and cresp.get("code") == 0):
                fleet_store.set_state(db, order_id, "provision_failed")
                info["provision_state"] = "provision_failed"
                warn(f"CANCEL UNCONFIRMED for {order_id}: "
                     "check the web UI immediately")
                return 1, info
            fleet_store.set_state(db, order_id, "cancelled")
            info["provision_state"] = "cancelled"
            say(f"order {order_id} cancelled (term not honored); "
                "verify any billed amount in the web UI")
            return 1, info

        # provision: bounded SSH retry, then credentials push, then a
        # sanitized status that must match our alias and pool
        info["stage"] = "provision"
        host, port = endpoint(order)
        deadline = time.monotonic() + PROVISION_SECONDS
        status = None
        while time.monotonic() < deadline:
            if host is None or port is None:
                try:
                    fresh = my_orders(api_key)
                    order = next((o for o in fresh
                                  if o.get("id") == order_id), order)
                    host, port = endpoint(order)
                except APIError:
                    pass
            if host and port:
                fleet_store.set_connection(db, order_id, host, port)
                try:
                    rig_ssh.push_credentials(host, port, key_path,
                                             order_id, creds)
                    raw = rig_ssh.ssh_json(host, port, key_path,
                                           order_id)
                    candidate = telemetry_policy.sanitize_status(
                        raw, expected_alias=alias)
                    if candidate["pool"] == pool:
                        status = candidate
                        break
                except (rig_ssh.SSHError, ValueError, KeyError):
                    pass
            time.sleep(PROVISION_GAP_SECONDS)
        if status is None:
            say(f"provisioning order {order_id} failed within "
                f"{PROVISION_SECONDS:.0f}s; auto-cancelling that "
                "exact order")
            cresp, cerr = _post_once("cancel_order", api_key,
                                     {"id": int(order_id)})
            final = ("cancelled" if not cerr and isinstance(cresp, dict)
                     and cresp.get("code") == 0 else "provision_failed")
            fleet_store.set_state(db, order_id, final)
            info["provision_state"] = final
            if cerr or not (isinstance(cresp, dict)
                            and cresp.get("code") == 0):
                warn(f"CANCEL UNCONFIRMED for {order_id}: "
                     "check the web UI immediately")
            return 1, info
        fleet_store.append_observation(db, order_id, status)
        fleet_store.set_state(db, order_id, "ready")
        info["provision_state"] = "ready"
        info["stage"] = "ready"
        say(f"order {order_id} READY at root@{host}:{port} "
            f"(pool={pool}, alias={alias}); background "
            "telemetry polls this rig")
        return 0, info
    finally:
        db.close()
        if lock:
            lock.close()


def cmd_create(args):
    live = bool(args.live)
    if not (1 <= args.hours <= rental_policy.MAX_HOURS):
        err("--hours must be 1..24")
        return 2
    try:
        ctx = quote_order(
            server_id=args.server, hours=args.hours,
            max_cost_usd=args.max_cost_usd, pool=args.pool,
            cpu_threads=args.cpu_threads, db_path=args.db,
            env_file=args.env_file, key_path=args.key,
            auto_renew=args.auto_renew)
    except ValueError:
        # quote_order already reported via warn (=err) -- identical
        # stderr text and exit code as before.
        return 2
    except APIError:
        return 1
    print_quote_summary(ctx["server"], ctx["quote"], ctx["pool"],
                        ctx["key_path"], ctx.get("blacklist"))
    if not live:
        print("\nDRY RUN -- no order placed. Re-run with --live and "
              "confirm to order.")
        return 0

    # confirmation
    if args.confirm_server is not None:
        if str(args.confirm_server) != str(args.server):
            err("--confirm-server does not match --server")
            return 2
    elif sys.stdin.isatty():
        answer = input(f"type `rent {ctx['server_id']}` to place this "
                       "order: ").strip()
        if answer != f"rent {ctx['server_id']}":
            print("not confirmed; no order placed")
            return 0
    else:
        err("non-interactive --live requires "
            f"--confirm-server {ctx['server_id']}")
        return 2

    rc, _info = create_order_flow(ctx)
    return rc


def cmd_list(args):
    db = connect_db(os.path.expanduser(args.db))
    try:
        fleet_store.initialize(db)
        rows = fleet_store.read_fleet(db)
        attempts = fleet_store.pending_attempts(db)
    finally:
        db.close()
    if attempts:
        print("unresolved attempts:")
        for p in attempts:
            print(f"  attempt {p['attempt_id']} server {p['server_id']} "
                  f"state {p['state']} started {p['created_at']}")
    if not rows:
        print("no rentals recorded")
        return 0
    print(f"{'order':>8} {'server':>8} {'state':<15} {'pool':<8} "
          f"{'alias':<24} {'deadline':<21} {'ssh':<22}")
    for r in rows:
        endpoint_ = (f"{r['ssh_host']}:{r['ssh_port']}"
                     if r.get("ssh_host") else "-")
        print(f"{r['order_id']:>8} {r['server_id']:>8} "
              f"{r['provision_state']:<15} {r['pool']:<8.8} "
              f"{r['alias']:<24.24} {str(r.get('deadline_at')):<21.21} "
              f"{endpoint_:<22}")
    return 0


def cancel_managed_order(order_id, db_path, env_file,
                         say=print, warn=err):
    """Cancel exactly one locally managed order: ONE cancel_order POST,
    no retry. Returns (rc, info). Unknown/observe-only ids refused."""
    db = _unguarded_db(db_path)
    try:
        row = db.execute(
            "SELECT * FROM fleet_rentals WHERE order_id=?",
            (int(order_id),)).fetchone()
        if row is None:
            warn(f"order {order_id} is not a locally managed rental; "
                 "observe-only orders are never cancelled here")
            return 2, {"order_id": order_id, "stage": "lookup"}
        if row["provision_state"] == "observe-only":
            warn("enrolled observe-only orders are never cancelled")
            return 2, {"order_id": order_id, "stage": "observe-only"}
        if row["provision_state"] in ("cancelled", "missing"):
            say(f"order {order_id} already ended "
                f"({row['provision_state']})")
            return 0, {"order_id": order_id,
                       "state": row["provision_state"]}
        api_key, _ = load_secrets(env_file, need_creds=False)
        say(f"order {order_id}: sending ONE cancel_order POST "
            "(no retries)...")
        resp, error = _post_once("cancel_order", api_key,
                                 {"id": int(order_id)})
        if error:
            warn(f"cancel POST failed ({error}); NO retry -- check "
                 "the web UI")
            return 1, {"order_id": order_id, "stage": "post"}
        if isinstance(resp, dict) and resp.get("code") == 0:
            fleet_store.set_state(db, order_id, "cancelled")
            say(f"order {order_id} cancelled")
            return 0, {"order_id": order_id, "state": "cancelled"}
        warn("cancel returned a failure code; check the web UI")
        return 1, {"order_id": order_id, "stage": "code"}
    finally:
        db.close()


def set_renewal(order_id, db_path, env_file, *, enable,
                max_price_day=None, say=print, warn=err):
    """Toggle auto-renew on a locally managed ACTIVE on-demand order:
    ONE set_order_auto_renew POST, never retried. Enabling requires a
    per-day price cap (auto_renew_max_price) so renewal stops if the
    host raises the price; spot/ended/observe-only orders refused."""
    info = {"order_id": order_id, "stage": "lookup"}
    if not isinstance(enable, bool):
        warn("enable must be a boolean")
        info["stage"] = "args"
        return 2, info
    cap = None
    if enable:
        if isinstance(max_price_day, bool) \
                or not isinstance(max_price_day, (int, float)) \
                or not math.isfinite(max_price_day) \
                or max_price_day <= 0:
            warn("enabling auto-renew requires a positive max $/day "
                 "cap (auto_renew_max_price)")
            info["stage"] = "cap"
            return 2, info
        cap = float(max_price_day)
    db = _unguarded_db(db_path)
    try:
        row = db.execute(
            "SELECT * FROM fleet_rentals WHERE order_id=?",
            (int(order_id),)).fetchone()
        if row is None:
            warn(f"order {order_id} is not a locally managed rental; "
                 "observe-only orders are never renewed here")
            return 2, info
        if row["provision_state"] == "observe-only":
            warn("enrolled observe-only orders are never renewed")
            info["stage"] = "observe-only"
            return 2, info
        if row["provision_state"] in ("cancelled", "missing",
                                      "provision_failed"):
            warn(f"order {order_id} already ended "
                 f"({row['provision_state']}); renewal cannot change")
            info["stage"] = "ended"
            return 2, info
        api_key, _ = load_secrets(env_file, need_creds=False)
        body = {"order_id": int(order_id), "auto_renew": enable}
        if cap is not None:
            body["auto_renew_max_price"] = cap
        say(f"order {order_id}: sending ONE set_order_auto_renew POST "
            "(no retries)...")
        resp, error = _post_once("set_order_auto_renew", api_key, body)
        if error:
            warn(f"auto-renew POST failed ({error}); NO retry -- "
                 "check the web UI")
            info["stage"] = "post"
            return 1, info
        if isinstance(resp, dict) and resp.get("code") == 0:
            fleet_store.set_auto_renew(db, int(order_id), enable)
            if enable:
                say(f"auto-renew ON for order {order_id} "
                    f"(cap ${cap}/day)")
            else:
                say(f"auto-renew OFF for order {order_id}")
            info["state"] = "on" if enable else "off"
            return 0, info
        detail = resp.get("error") if isinstance(resp, dict) else None
        if detail is None:
            detail = resp.get("code") if isinstance(resp, dict) \
                else "no response"
        warn(f"auto-renew returned a failure ({str(detail)}); check "
             "the web UI")
        info["stage"] = "code"
        return 1, info
    finally:
        db.close()


def cmd_cancel(args):
    db = connect_db(os.path.expanduser(args.db))
    try:
        fleet_store.initialize(db)
        row = db.execute(
            "SELECT * FROM fleet_rentals WHERE order_id=?",
            (args.order,)).fetchone()
        if row is None:
            err(f"order {args.order} is not a locally managed rental; "
                "observe-only orders are never cancelled here")
            return 2
        if row["provision_state"] == "observe-only":
            err("enrolled observe-only orders are never cancelled")
            return 2
        if row["provision_state"] == "cancelled":
            print(f"order {args.order} already cancelled")
            return 0
        print(f"cancel order {args.order} (server {row['server_id']}, "
              f"state {row['provision_state']})")
        if not args.live:
            print("DRY RUN -- re-run with --live and confirm")
            return 0
        if not sys.stdin.isatty():
            err("cancel requires an interactive `cancel "
                f"{args.order}` confirmation")
            return 2
        if input(f"type `cancel {args.order}` to confirm: ").strip() \
                != f"cancel {args.order}":
            print("not confirmed")
            return 0
        rc, _info = cancel_managed_order(
            args.order, args.db, args.env_file)
        return rc
    finally:
        db.close()


def cmd_enroll(args):
    db = connect_db(os.path.expanduser(args.db))
    try:
        fleet_store.initialize(db)
        api_key, _ = load_secrets(args.env_file, need_creds=False)
        try:
            current = my_orders(api_key)
        except APIError as exc:
            err(f"cannot verify order on the account: {exc}")
            return 1
        match = next((o for o in current if o.get("id") == args.order),
                     None)
        if match is None:
            err(f"order {args.order} not found on this account; "
                "refusing to enroll")
            return 2
        existing = db.execute(
            "SELECT order_id FROM fleet_rentals WHERE order_id=?",
            (args.order,)).fetchone()
        if existing:
            err(f"order {args.order} already recorded")
            return 2
        if not isinstance(args.host, str) \
                or not HOST_RE.fullmatch(args.host) \
                or ".." in args.host:
            err("invalid --host")
            return 2
        if not 1 <= args.port <= 65535:
            err("invalid --port")
            return 2
        key_path = os.path.expanduser(args.key)
        if not os.path.isfile(key_path):
            err(f"ssh key not found: {key_path}")
            return 2
        fleet_store.register_rental(
            db, order_id=args.order, server_id=match["si"],
            alias=f"rig-observe-{args.order}", pool="unknown",
            deadline_at=None, max_cost_usd=None, quoted_cost_usd=None,
            ssh_key_path=key_path)
        fleet_store.set_connection(db, args.order, args.host, args.port)
        fleet_store.set_state(db, args.order, "observe-only")
        print(f"order {args.order} enrolled observe-only at "
              f"{args.host}:{args.port}; no deployment, no tokens")
        return 0
    finally:
        db.close()


def cmd_reconcile(args):
    db = connect_db(os.path.expanduser(args.db))
    try:
        fleet_store.initialize(db)
        pending = fleet_store.pending_attempts(db)
        if not pending:
            print("no unresolved attempts")
            return 0
        api_key, _ = load_secrets(args.env_file, need_creds=False)
        try:
            current = my_orders(api_key)
        except APIError as exc:
            err(f"inventory unavailable: {exc}")
            return 1
        rc = 0
        adopted = adopt_seen = False
        for p in pending:
            before = set(json.loads(p["before_ids_json"]))
            quote = json.loads(p["quote_json"])
            found = [o for o in current
                     if o["id"] not in before
                     and o.get("si") == p["server_id"]]

            if args.clear_empty == p["attempt_id"]:
                if found:
                    err(f"attempt {p['attempt_id']} has {len(found)} "
                        "new-order candidate(s); cannot clear-empty")
                    rc = 2
                    continue
                if not sys.stdin.isatty():
                    err("--clear-empty needs interactive confirmation")
                    rc = 2
                    continue
                if input(f"type `no order {p['attempt_id']}` to clear: "
                         ).strip() != f"no order {p['attempt_id']}":
                    print("not confirmed")
                    continue
                fleet_store.finish_attempt(db, p["attempt_id"],
                                           "no_order")
                print(f"attempt {p['attempt_id']} cleared (no_order)")
                continue

            candidate = next(
                (o for o in found if o["id"] == args.adopt_order),
                None) if args.adopt_order is not None else None
            if args.adopt_order is not None and candidate is None:
                continue  # not this attempt's candidate
            if candidate is None and len(found) == 1:
                # unique candidate: offer explicit adoption only
                candidate = found[0]
                ok = False
                if sys.stdin.isatty():
                    ok = input(
                        f"attempt {p['attempt_id']}: candidate order "
                        f"{candidate['id']} on server {p['server_id']}. "
                        f"type `adopt {candidate['id']}` to mark it "
                        "managed: ").strip() == \
                        f"adopt {candidate['id']}"
                if not ok:
                    print(f"attempt {p['attempt_id']}: candidate "
                          f"{candidate['id']} NOT adopted. Verify it is "
                          "yours in the web UI, then `python3 rent.py "
                          f"reconcile --adopt-order "
                          f"{candidate['id']}` (no remote action here).")
                    candidate = None
                    rc = 1
            if candidate is not None:
                if args.adopt_order is not None:
                    adopt_seen = True
                if not _term_matches(candidate, quote.get("hours"),
                                     quote.get("auto_renew") is True):
                    print(f"candidate {candidate['id']} lacks the "
                          "stored bounded term (rental_hours/"
                          "auto_renew) -- stays unknown; review and "
                          "cancel in the web UI if it is not yours")
                    rc = 1
                    continue
                if _register_managed(db, p["attempt_id"], candidate,
                                     p["server_id"], p["alias"], quote,
                                     "?"):
                    adopted = True
                    print(f"attempt {p['attempt_id']} adopted order "
                          f"{candidate['id']} (recorded only; no "
                          "deploy, no SSH, no cancel)")
                else:
                    rc = 1
                continue
            if not found:
                print(f"attempt {p['attempt_id']}: no new order on "
                      f"server {p['server_id']} -- still unresolved; "
                      "check the web UI, or `reconcile --clear-empty` "
                      "only if you are sure no order was created")
            else:
                ids = [o["id"] for o in found]
                print(f"attempt {p['attempt_id']}: {len(found)} "
                      f"candidates {ids} -- adopt one explicitly with "
                      "`reconcile --adopt-order ID` after checking the "
                      "web UI")
            rc = 1
        if args.adopt_order is not None and not adopted \
                and not adopt_seen:
            err(f"order {args.adopt_order} is not an unresolved "
                "candidate for any pending attempt")
            rc = 2
        return rc
    finally:
        db.close()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Operator rental CLI. create is a DRY RUN unless "
                    "--live is given.")
    sub = parser.add_subparsers(dest="action", required=True)

    sp = sub.add_parser("create", help="quote + optionally place an order")
    sp.add_argument("--server", type=int, required=True)
    sp.add_argument("--hours", type=int, default=1)
    sp.add_argument("--max-cost-usd", default=None,
                    help="optional ceiling; otherwise confirm the fresh quoted bound")
    sp.add_argument("--pool", choices=["jetski", "qli"], default="jetski")
    sp.add_argument("--auto-renew", action="store_true",
                    help="renew the order each term until cancelled; "
                         "the cost bound covers ONE term only")
    sp.add_argument("--cpu-threads", type=int, default=None)
    sp.add_argument("--db", default=DEFAULT_DB)
    sp.add_argument("--env-file", default=DEFAULT_ENV)
    sp.add_argument("--key", default=DEFAULT_KEY,
                    help="dedicated clorqub ssh private key path")
    sp.add_argument("--live", action="store_true",
                    help="actually place the order after confirmation")
    sp.add_argument("--confirm-server", type=int, default=None,
                    help="non-interactive confirmation; must equal "
                         "--server")

    sp = sub.add_parser("list", help="local rental records (read-only)")
    sp.add_argument("--db", default=DEFAULT_DB)

    sp = sub.add_parser("cancel", help="cancel a locally managed order")
    sp.add_argument("--order", type=int, required=True)
    sp.add_argument("--db", default=DEFAULT_DB)
    sp.add_argument("--env-file", default=DEFAULT_ENV)
    sp.add_argument("--live", action="store_true")

    sp = sub.add_parser("enroll",
                        help="observe-only enrollment of an existing "
                             "order (no deploy, no tokens)")
    sp.add_argument("--order", type=int, required=True)
    sp.add_argument("--host", required=True)
    sp.add_argument("--port", type=int, required=True)
    sp.add_argument("--key", default=DEFAULT_KEY)
    sp.add_argument("--db", default=DEFAULT_DB)
    sp.add_argument("--env-file", default=DEFAULT_ENV)

    sp = sub.add_parser("reconcile", help="resolve pending attempts")
    sp.add_argument("--db", default=DEFAULT_DB)
    sp.add_argument("--env-file", default=DEFAULT_ENV)
    sp.add_argument("--clear-empty", metavar="ATTEMPT", default=None,
                    help="mark an attempt no_order after interactive "
                         "confirmation (requires zero candidates)")
    sp.add_argument("--adopt-order", type=int, default=None,
                    help="explicitly mark a candidate order managed "
                         "for a pending attempt")

    args = parser.parse_args(argv)
    handler = {"create": cmd_create, "list": cmd_list,
               "cancel": cmd_cancel, "enroll": cmd_enroll,
               "reconcile": cmd_reconcile}[args.action]
    try:
        return handler(args)
    except APIError as exc:
        err(f"API error: {exc}")
        return 1
    except ValueError as exc:
        err(str(exc))
        return 2
    except (OSError, sqlite3.Error):
        err("storage/system error")
        return 2
    except Exception as exc:
        # never echo raw exception text that could carry secrets
        err(f"unexpected {type(exc).__name__}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
