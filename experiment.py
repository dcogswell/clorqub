"""Experiment-phase auto-rental runner (see PLAN.md "Benchmarking and
auto-rental").

A `phase` is a bounded experiment window with a USD budget. `run`
watches the phase's experiment rentals (which the collector observes
into rig_observations) and applies re-roll rules: fault cancels
inside the free window, and dry-tree handling via bursts.
A produced slot that goes quiet for its x arm triggers a probe burst
while it keeps hashing; at judge time the incumbent is re-checked and
either keeps its slot, or is replaced by a promoted probe (its order
keeps the proven tree). When nothing produced, the incumbent is
cancelled and a retry burst refills the vacancy after the y arm.
Dry-run is the default; --live spends money and requires
--confirm-budget.

Only orders recorded in experiment_rentals are ever acted on.
"""

import argparse
import fcntl
import itertools
import json
import math
import os
import re
import signal
import statistics
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING

import blacklist
import fleet_store
import market
import orders
import pool_stats
import rent
import rental_policy
import spot
import trees
from cli import DEFAULT_DB, DEFAULT_ENV

SCHEMA = """
CREATE TABLE IF NOT EXISTS experiment_phases (
    phase_id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    budget_usd TEXT NOT NULL,
    reserve_usd TEXT NOT NULL,
    config_json TEXT NOT NULL,
    closed_at TEXT
);
CREATE TABLE IF NOT EXISTS experiment_rentals (
    order_id INTEGER PRIMARY KEY,
    phase_id INTEGER NOT NULL,
    server_id INTEGER NOT NULL,
    tier TEXT NOT NULL,
    reason TEXT NOT NULL,
    x_min INTEGER NOT NULL,
    y_min INTEGER NOT NULL,
    hourly_usd TEXT NOT NULL,
    creation_fee_usd TEXT NOT NULL,
    quoted_cost_usd TEXT NOT NULL,
    created_at TEXT NOT NULL,
    ended_at TEXT,
    end_reason TEXT,
    role TEXT NOT NULL DEFAULT 'slot',
    burst_id INTEGER
);
CREATE TABLE IF NOT EXISTS experiment_bursts (
    burst_id INTEGER PRIMARY KEY,
    phase_id INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    trigger TEXT NOT NULL,
    trigger_order_id INTEGER,
    n_target INTEGER NOT NULL,
    judged_at TEXT
);
CREATE TABLE IF NOT EXISTS experiment_events (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    phase_id INTEGER NOT NULL,
    order_id INTEGER,
    server_id INTEGER,
    kind TEXT NOT NULL,
    detail_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS probe_pins (
    phase_id INTEGER NOT NULL,
    server_id INTEGER NOT NULL,
    tagged_at TEXT NOT NULL,
    PRIMARY KEY (phase_id, server_id)
);
CREATE TABLE IF NOT EXISTS tree_reports (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    source TEXT NOT NULL,
    epoch INTEGER,
    tree_id TEXT NOT NULL,
    seed TEXT,
    its REAL,
    shares INTEGER,
    detail_json TEXT
);
CREATE INDEX IF NOT EXISTS tree_reports_ts ON tree_reports(ts);
"""

ENDED_STATES = ("cancelled", "missing", "provision_failed")
POOL = "jetski"
ZEN_RE = re.compile(
    r"\bRyzen \d+ (?:7\d{2}[05]|9\d{2}[05])(?:X3D|X|HX3D|HX)?\b")
CANCEL_RETRY_MIN = 10
# A rent blocks inside create_order_flow for up to the provision wait;
# freeze new rentals this long before any probe's free-window deadline
# so the wait can't push a deadline cancel past the window.
PROBE_RENT_GUARD_MIN = 4
POOL_VALUE_MAX_AGE_MIN = 30

DEFAULT_CONFIG = {
    "max_rentals": 2,
    "hours": 4,
    "max_price_hr": "0.50",
    "x_min": [30, 60, 90],
    "y_min": [15, 30, 60],
    "startup_min": 8,
    "startup_restart_every_min": 2,
    "fault_check_min": 6,
    "free_window_min": 10,
    "dry_after_min": 15,
    "yield_floor_frac": 0.25,
    # the trailing-window floor is looser about noise than the
    # 8-10min early gate: measured clusters put bad draws <=0.45 of
    # predicted and healthy rigs >=0.68, so the x/reroll check sits
    # in the gap at 0.5
    "yield_x_floor_frac": 0.5,
    # past this age a slot's score is its measured yield -- realized
    # shares/h priced at the pool's current counted value -- not the
    # its/s forecast; luck has averaged enough by then to be real
    "actual_yield_min": 25,
    "unreachable_min": 15,
    # a listing that failed quote stays out of the candidate pool for
    # this long -- repeated attempts against the same dead listing
    # burned whole upgrade windows before
    "quote_reject_min": 60,
    # a rent attempt parks in 'unknown' when its POST's outcome cannot
    # be reconciled (the order may exist -- double-renting must be
    # impossible), and parking blocks all actions. Recheck parked
    # attempts older than this many minutes against the live order
    # list at this same interval; an in-flight attempt finishes well
    # inside this window so parked means stuck. After
    # attempt_recheck_clear consecutive clean fetches show no new
    # order on the server, the POST provably created nothing and the
    # attempt auto-resolves to 'no_order' -- a found candidate keeps
    # it parked since adopting an ambiguous order stays manual
    "attempt_recheck_min": 2,
    "attempt_recheck_clear": 3,
    # minimum Clore-reported server reliability to rent; a listing
    # with no rating at all fails this too
    "min_reliability": 0.75,
    # an upgrade burst that judged without promoting cools its target
    # challenger for this long -- otherwise a dominant-on-paper but
    # unrentable listing re-triggers an audition every window forever.
    # The same window gates re-audition of a confirmed tree-miss
    # probe: both its confirmed end and its judgement must be this old
    "upgrade_cooldown_min": 60,
    # max actual upgrade rentals per server per phase, initial
    # attempt included: bounds how often a confirmed healthy
    # no-share tree miss may be re-auditioned
    "upgrade_tree_max_attempts": 3,
    "rerent_grace_min": 60,
    "stale_obs_sec": 120,
    "stale_market_min": 15,
    "burst_max": 6,
    "burst_fill_min": 5,
    # minutes before a slot's term deadline to audition successors:
    # sequential probe fills plus provisioning need the lead time,
    # and the auto-renew fallback must land before the final minute
    "succession_min": 20,
    # pool-wide lull guard: this many consecutive yield-failure
    # endings pauses new slot fills for lull_pause_min -- during a
    # lull every fresh fill pays the creation fee just to rediscover
    # the pool is dry. Probes are exempt: they die inside the free
    # window anyway, and their yield doubles as a liveness check.
    "lull_streak": 2,
    "lull_pause_min": 45,
    "probe_judge_min": 8,
    "probe_cancel_by_min": 9,
    # CPU miner threads contend with the GPU feed path for memory
    # bandwidth (measured +17% gpu it/s on an AVX2 i5 when cut from
    # listed-1 to 3). Default rentals cap mining threads at this;
    # 0 restores the listed-1 convention, explicit orders override.
    "cpu_threads_cap": 4,
    # CPU model regex excluded from auto candidates: EPYC series
    # 1-8 are AVX2-only with per-thread clocks too slow for this
    # algo, and the many-core boxes feed the GPU worst. The 9xxx
    # series (Zen4/5: 9654/9754/9455P/9255...) has AVX512 + high
    # clocks and stays eligible. Manual rentals are unaffected.
    "cpu_blocklist_re": r"\bEPYC\s+[1-8]",
    # target pool-window share (percent of pool total_sol): 0 = off.
    # The Experiment tab prices holding the share from live pool
    # telemetry plus the marketplace its-supply curve; the operator
    # approves the estimate, which funds it as the phase budget.
    # While enabled + approved + capacity-deficit, the candidate
    # price cap lifts from max_price_hr to pool_share_max_price_hr
    # so throughput rigs can catch up -- value gates still apply.
    "pool_share_enabled": False,
    "pool_share_target_pct": 0.0,
    "pool_share_max_price_hr": "2.00",
    "pool_share_reapprove_drift": 0.25,
    # approval snapshot -- written by approve_pool_share, not user
    # input: the funded estimate the runner defends against
    "pool_share_approved_usd": 0.0,
    "pool_share_approved_hr": 0.0,
    "pool_share_approved_pct": 0.0,
    "pool_share_approved_at": "",
    # Poisson kill evidence: dry/low-yield verdicts wait for
    # kill_lambda expected shares to have elapsed -- P(0 observed |
    # healthy) ~= e^-lambda -- so judgement windows stretch with
    # share scarcity instead of a fixed clock. kill_lambda_free is
    # the looser standard inside the free-cancel window: a slightly
    # higher false-kill rate is the price of staying free.
    "kill_lambda": 3.0,
    "kill_lambda_free": 2.2,
    # promotion: a probe replaces a dry slot only when within
    # promote_its_floor of the best its and ahead on its/$ by margin
    "promote_its_floor": 0.5,
    "promote_margin": 0.15,
    # Upgrade margin applies to net USD/hour when calibrated, relative
    # value/$ otherwise; a producing measured probe must confirm it.
    "upgrade_margin": 0.25,
    # Under score=gross only: gross $/h within this fraction counts
    # as a near-tie, and the contest is arbitrated on net instead --
    # the cheaper earner of the same revenue is the net winner.
    "gross_tiebreak": 0.15,
    # its_total is the median of good samples inside this trailing
    # window -- instantaneous reads swing too wildly to judge on
    "its_window_min": 15,
    # Provisional diminishing-return curve, not a measured payout law.
    # Wallet-level pool snapshots cannot identify per-worker weighting.
    "its_exponent": 0.3,
    # the yardstick candidates and incumbents are ranked on, applied
    # uniformly to both sides: "net" (default, current behavior) =
    # predicted/measured $/h after fee-inclusive cost, "gross" =
    # predicted/measured revenue $/h ignoring cost, "its" = it/s
    # ignoring price. Named bundles live in STRATEGIES
    # (`experiment.py set strategy=<name>`); they overwrite the
    # bundled gate keys below, individual `set` calls still win after.
    "score": "net",
    # efficiency floor: never rent (slot or probe) a listing whose
    # value/$ is below this share of the best available listing's.
    # Keep this protection while dollar forecasts remain provisional.
    "min_value_ratio": 0.7,
    # absolute return gate: once the pool-snapshot value model is
    # available, a listing must predict at least this many $ of
    # payout per $ of fee-inclusive cost or it is never rented
    "min_return": 1.2,
    # seat floor that survives loss_mining: even when every finder
    # waves through _econ, a seat is a standing cost commitment --
    # a probe only earns one when its measured return clears this
    # floor over the remaining term, fee included. Below 1.0 it
    # deliberately seats net losers for pool presence; 0.6 says
    # "some value back" without demanding profit.
    "min_seat_return": 0.6,
    # probe-pick diversity floor: a GPU class with fewer phase
    # rental samples than this outranks higher-forecast picks from
    # well-measured classes, so priors get a cross-section of
    # actuals. Once a class hits the threshold its candidates
    # compete on score alone -- sampling finds the efficiency
    # floor, then the floor decides.
    "gpu_trial_min": 3,
    # when every listing fails the floor/return/budget gates, a
    # vacancy still fills with the best below-floor rig: the upgrade
    # scan replaces it the moment a stronger listing frees, and an
    # unmeasured rig pays in data even when its estimate was right.
    # Off by default -- below-floor rigs burn at a worse rate, so it
    # is the operator's call whether the current QU price justifies
    # mining at a loss. Toggle live via `experiment.py set` or the
    # TUI; the runner re-reads config each cycle.
    "loss_mining": False,
    # fallback shares per rig-hour for the value model when this
    # phase has under 6 rig-hours of slot history
    "default_shares_per_hour": 300,
    # dynamic spot-vs-on-demand selection, dark-shipped: while off,
    # every quote/order is on-demand exactly as before. Spot orders
    # bill per minute at a bid and vanish when outbid, so a slot fill
    # only takes spot when the fee-inclusive hourly clears the on-
    # demand price by spot_min_discount; probes get their own (looser)
    # threshold since the spot creation fee is 20x cheaper. A server
    # whose spot rental ended outbid is spot-cooled for
    # spot_outbid_min -- it stays on-demand eligible.
    "spot_enabled": False,
    "spot_min_discount": 0.10,
    "spot_probe_min_discount": 0.0,
    # per-role spot posture: "auto" takes spot whenever its quote
    # clears the discount threshold and moves on when the pre-POST
    # recheck finds the market moved; "prefer" retries the same server
    # on-demand instead of moving on; "only" drops candidates with no
    # usable spot quote entirely (auditions only happen at spot's
    # $0.005 fee and free cancels); "off" never quotes spot. "prefer"
    # costs the on-demand price on aborts -- right for probes, where
    # the server is the point and an audition lost to a bid tick
    # wastes the whole pick cycle
    "spot_probe_mode": "auto",
    "spot_slot_mode": "auto",
    # bid = floor, or premium x the top competing bid; Clore rejects
    # anything under 1.01x. Raising it buys fewer recheck aborts on
    # contested servers at the cost of a thinner discount
    "spot_bid_premium": 1.01,
    "spot_outbid_min": 60,
    "spot_probe_judge_min": 20,
    # operator pause: blocks every new rent/burst while cancels,
    # judges and retry bookkeeping keep running. For the day the pool
    # algo changes and evidence needs a reset before spend resumes
    "paused": False,
    # probe master switch: off suppresses every burst trigger (dry,
    # manual, succession, upgrade, dry_retry) while slot fills,
    # cancels and in-flight judgements keep running -- the fleet
    # rides its current slots instead of paying for auditions
    "probes_enabled": True,
    # fleet sizing: "static" always fills to max_rentals; "dynamic"
    # targets min(max_rentals, max(slot_min, exceptional_slots + 1))
    # -- each rig standing out above the fleet's mean score by
    # growth_margin earns one hunting seat beyond itself, so the
    # fleet grows only on exceptional trees and settles to slot_min
    # during normal/lean stretches
    "slot_mode": "dynamic",
    "slot_min": 2,
    "growth_margin": 0.25,
    # how an over-target fleet contracts: "passive" just stops
    # refills (attrition only); "no_renew" also denies succession
    # auditions and pending rerents to the weakest excess slots so
    # they exit at term end; "cull" actively cancels the weakest
    # below-average excess slot each cycle
    "shrink_mode": "passive",
    # worker-identity reuse: rentals draw a fresh pool tree either way
    # (measured 2026-11-17: the pin does not survive worker death --
    # 0/3 recipients got the donor's tree back), so reusing an ended
    # rental's alias buys dashboard hygiene, not tree luck. The only
    # gate is that the identity be provably dead: never a live worker,
    # a pending attempt, or a resumable outbid spot order.
    "reuse_aliases": True,
}

# Named strategy bundles for `set strategy=<name>`: each overwrites
# the score yardstick plus the gate/margin keys it bundles. `value`
# reproduces today's defaults; `aggressive` and `throughput` trade
# progressively looser gates and lower margins for faster churn on a
# different score. Individual `set` calls after a bundle still win.
STRATEGIES = {
    "value": {"score": "net", "min_return": 1.2, "min_value_ratio": 0.7,
              "upgrade_margin": 0.25, "promote_margin": 0.15},
    "aggressive": {"score": "gross", "min_return": 1.05,
                   "min_value_ratio": 0.5, "upgrade_margin": 0.15,
                   "promote_margin": 0.1},
    "throughput": {"score": "its", "min_return": 1.0,
                   "min_value_ratio": 0.3, "upgrade_margin": 0.1,
                   "promote_margin": 0.05},
}
# one yardstick for candidates and incumbents alike: candidates are
# scored on predicted yield, incumbents on measured yield
SCORE_HELP = {
    "net": "net $/h -- revenue minus fee-inclusive rental cost; "
           "picks the most profitable rig",
    "gross": "gross $/h -- revenue before cost; picks the biggest "
             "earner the pace and gates allow, even at worse $/share",
    "its": "it/s -- raw throughput; ignores price and share value "
           "entirely",
}


def _dt(value):
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


def _iso(value):
    return value.isoformat() if isinstance(value, datetime) else value


def _minutes(n):
    return timedelta(minutes=n)


def _dec(value, default="0"):
    return Decimal(str(value)) if value is not None else Decimal(default)


def utcnow():
    return datetime.now(timezone.utc).replace(microsecond=0)


EPOCH_END_WEEKDAY = 2  # Qubic epochs transition Wednesday 12:00 UTC


def epoch_end(now):
    """Next Wednesday 12:00 UTC after `now`."""
    days = (EPOCH_END_WEEKDAY - now.weekday()) % 7
    end = (now + timedelta(days=days)).replace(
        hour=12, minute=0, second=0, microsecond=0)
    if end <= now:
        end += timedelta(days=7)
    return end


_EPOCH_REF = datetime(2020, 1, 1, 12, tzinfo=timezone.utc)  # a Wednesday noon


def _epoch_idx(ts):
    """Qubic epoch number for a timestamp: weeks on the
    Wednesday-12:00 grid. Observations and priors tag with this so
    post-algo-change data never blends with pre-change data."""
    return (ts - _EPOCH_REF).days // 7


def open_db(path):
    db = market.connect_db(os.path.expanduser(path))
    fleet_store.initialize(db)
    blacklist.initialize(db)
    db.executescript(SCHEMA)
    pool_stats.initialize(db)
    # migration: role/burst_id on experiment_rentals for burst probing
    cols = {row[1] for row in
            db.execute("PRAGMA table_info(experiment_rentals)")}
    if "role" not in cols:
        db.execute(
            "ALTER TABLE experiment_rentals ADD COLUMN role "
            "TEXT NOT NULL DEFAULT 'slot'")
    if "burst_id" not in cols:
        db.execute(
            "ALTER TABLE experiment_rentals ADD COLUMN "
            "burst_id INTEGER")
    if "mode" not in cols:
        db.execute(
            "ALTER TABLE experiment_rentals ADD COLUMN mode "
            "TEXT NOT NULL DEFAULT 'on-demand'")
    if "resumed_at" not in cols:
        db.execute(
            "ALTER TABLE experiment_rentals ADD COLUMN "
            "resumed_at TEXT")
    if "reused_from" not in cols:
        db.execute(
            "ALTER TABLE experiment_rentals ADD COLUMN "
            "reused_from INTEGER")
    db.commit()
    return db


def validate_config(cfg):
    cfg = {**DEFAULT_CONFIG, **cfg}
    if cfg["max_rentals"] < 1 or \
            cfg["max_rentals"] > rental_policy.MAX_AUTO_RENTALS:
        raise ValueError(
            f"max_rentals must be 1..{rental_policy.MAX_AUTO_RENTALS}")
    if isinstance(cfg["hours"], bool) or not isinstance(cfg["hours"], int) \
            or not 1 <= cfg["hours"] <= rental_policy.MAX_HOURS:
        raise ValueError(
            f"hours must be 1..{rental_policy.MAX_HOURS}")
    if not cfg["x_min"] or not cfg["y_min"]:
        raise ValueError("x_min/y_min lists must be nonempty")
    _dec(cfg["max_price_hr"])
    if cfg["burst_max"] < 1 or \
            cfg["burst_max"] > rental_policy.MAX_BURST_PROBES:
        raise ValueError(
            f"burst_max must be 1..{rental_policy.MAX_BURST_PROBES}")
    if not 0 < cfg["promote_its_floor"] <= 1:
        raise ValueError("promote_its_floor must be in (0, 1]")
    if not 0 <= cfg["promote_margin"]:
        raise ValueError("promote_margin must be >= 0")
    if not 0 <= cfg["upgrade_margin"]:
        raise ValueError("upgrade_margin must be >= 0")
    if not math.isfinite(cfg["gross_tiebreak"]) \
            or not 0 <= cfg["gross_tiebreak"] < 1:
        raise ValueError("gross_tiebreak must be in [0, 1)")
    if cfg["upgrade_cooldown_min"] < 0:
        raise ValueError("upgrade_cooldown_min must be >= 0")
    if isinstance(cfg["upgrade_tree_max_attempts"], bool) \
            or not isinstance(cfg["upgrade_tree_max_attempts"], int) \
            or cfg["upgrade_tree_max_attempts"] < 1:
        raise ValueError(
            "upgrade_tree_max_attempts must be an int >= 1")
    if cfg["its_window_min"] < 1:
        raise ValueError("its_window_min must be >= 1")
    if cfg["succession_min"] < 5:
        raise ValueError("succession_min must be >= 5")
    if cfg["lull_streak"] < 1:
        raise ValueError("lull_streak must be >= 1")
    if cfg["lull_pause_min"] < 0:
        raise ValueError("lull_pause_min must be >= 0")
    if not 0 < cfg["yield_floor_frac"] < 1:
        raise ValueError("yield_floor_frac must be in (0, 1)")
    if not 0 < cfg["yield_x_floor_frac"] < 1:
        raise ValueError("yield_x_floor_frac must be in (0, 1)")
    if cfg["actual_yield_min"] < 1:
        raise ValueError("actual_yield_min must be >= 1")
    if not 0 < cfg["its_exponent"] <= 1:
        raise ValueError("its_exponent must be in (0, 1]")
    if not 0 <= cfg["min_value_ratio"] <= 1:
        raise ValueError("min_value_ratio must be in [0, 1]")
    if not math.isfinite(cfg["min_return"]) or cfg["min_return"] < 0:
        raise ValueError("min_return must be >= 0")
    if not math.isfinite(cfg["min_seat_return"]) \
            or cfg["min_seat_return"] < 0:
        raise ValueError("min_seat_return must be >= 0")
    if isinstance(cfg["gpu_trial_min"], bool) \
            or not isinstance(cfg["gpu_trial_min"], int) \
            or cfg["gpu_trial_min"] < 0:
        raise ValueError("gpu_trial_min must be an int >= 0")
    if cfg["score"] not in ("net", "gross", "its"):
        raise ValueError("score must be net, gross or its")
    if not math.isfinite(cfg["default_shares_per_hour"]) \
            or cfg["default_shares_per_hour"] <= 0:
        raise ValueError("default_shares_per_hour must be > 0")
    if cfg["probe_cancel_by_min"] >= cfg["free_window_min"]:
        raise ValueError(
            "probe_cancel_by_min must be < free_window_min")
    if isinstance(cfg["cpu_threads_cap"], bool) \
            or not isinstance(cfg["cpu_threads_cap"], int) \
            or not 0 <= cfg["cpu_threads_cap"] <= 512:
        raise ValueError("cpu_threads_cap must be an int 0..512")
    try:
        re.compile(cfg["cpu_blocklist_re"] or "")
    except re.error as exc:
        raise ValueError(f"cpu_blocklist_re: {exc}") from None
    if not isinstance(cfg["pool_share_enabled"], bool):
        raise ValueError("pool_share_enabled must be a boolean")
    if not isinstance(cfg["pool_share_target_pct"], (int, float)) \
            or isinstance(cfg["pool_share_target_pct"], bool) \
            or not 0 <= cfg["pool_share_target_pct"] <= 100:
        raise ValueError("pool_share_target_pct must be a number 0..100")
    _dec(cfg["pool_share_max_price_hr"])
    if not isinstance(cfg["pool_share_approved_usd"], (int, float)) \
            or isinstance(cfg["pool_share_approved_usd"], bool) \
            or cfg["pool_share_approved_usd"] < 0:
        raise ValueError("pool_share_approved_usd must be >= 0")
    if not isinstance(cfg["pool_share_approved_hr"], (int, float)) \
            or isinstance(cfg["pool_share_approved_hr"], bool) \
            or cfg["pool_share_approved_hr"] < 0:
        raise ValueError("pool_share_approved_hr must be >= 0")
    if not isinstance(cfg["pool_share_approved_pct"], (int, float)) \
            or isinstance(cfg["pool_share_approved_pct"], bool) \
            or not 0 <= cfg["pool_share_approved_pct"] <= 100:
        raise ValueError("pool_share_approved_pct must be a number 0..100")
    if not isinstance(cfg["pool_share_approved_at"], str):
        raise ValueError("pool_share_approved_at must be a string")
    if not math.isfinite(cfg["pool_share_reapprove_drift"]) \
            or not 0 < cfg["pool_share_reapprove_drift"] <= 1:
        raise ValueError("pool_share_reapprove_drift must be in (0, 1]")
    if not math.isfinite(cfg["kill_lambda"]) or cfg["kill_lambda"] <= 0:
        raise ValueError("kill_lambda must be > 0")
    if not math.isfinite(cfg["kill_lambda_free"]) \
            or cfg["kill_lambda_free"] <= 0:
        raise ValueError("kill_lambda_free must be > 0")
    if cfg["startup_min"] < 0:
        raise ValueError("startup_min must be >= 0")
    if cfg["dry_after_min"] < 1:
        raise ValueError("dry_after_min must be >= 1")
    if cfg["quote_reject_min"] < 0:
        raise ValueError("quote_reject_min must be >= 0")
    if cfg["attempt_recheck_min"] < 1:
        raise ValueError("attempt_recheck_min must be >= 1")
    if cfg["attempt_recheck_clear"] < 1:
        raise ValueError("attempt_recheck_clear must be >= 1")
    if not 0 <= cfg["min_reliability"] <= 1:
        raise ValueError("min_reliability must be in [0, 1]")
    if not isinstance(cfg["spot_enabled"], bool):
        raise ValueError("spot_enabled must be a boolean")
    if not isinstance(cfg["loss_mining"], bool):
        raise ValueError("loss_mining must be a boolean")
    if not math.isfinite(cfg["spot_min_discount"]) \
            or not 0 <= cfg["spot_min_discount"] <= 1:
        raise ValueError("spot_min_discount must be in [0, 1]")
    if not math.isfinite(cfg["spot_probe_min_discount"]) \
            or not 0 <= cfg["spot_probe_min_discount"] <= 1:
        raise ValueError("spot_probe_min_discount must be in [0, 1]")
    for key in ("spot_probe_mode", "spot_slot_mode"):
        if cfg[key] not in ("auto", "prefer", "only", "off"):
            raise ValueError(f"{key} must be auto, prefer, only or off")
    if not math.isfinite(cfg["spot_bid_premium"]) \
            or not 1.0 <= cfg["spot_bid_premium"] <= 1.5:
        raise ValueError("spot_bid_premium must be in [1.0, 1.5]")
    if not isinstance(cfg["paused"], bool):
        raise ValueError("paused must be a boolean")
    if not isinstance(cfg["probes_enabled"], bool):
        raise ValueError("probes_enabled must be a boolean")
    if cfg["slot_mode"] not in ("static", "dynamic"):
        raise ValueError("slot_mode must be static or dynamic")
    if not isinstance(cfg["slot_min"], int) \
            or isinstance(cfg["slot_min"], bool) or cfg["slot_min"] < 0:
        raise ValueError("slot_min must be an int >= 0")
    # the floor can't exceed the ceiling; clamp rather than reject so
    # a config written before the ceiling was lowered still loads
    cfg["slot_min"] = min(cfg["slot_min"], cfg["max_rentals"])
    if not math.isfinite(cfg["growth_margin"]) \
            or not 0 <= cfg["growth_margin"] <= 10:
        raise ValueError("growth_margin must be in [0, 10]")
    if cfg["shrink_mode"] not in ("passive", "no_renew", "cull"):
        raise ValueError("shrink_mode must be passive, no_renew or cull")
    if cfg["spot_outbid_min"] < 0:
        raise ValueError("spot_outbid_min must be >= 0")
    if cfg["spot_probe_judge_min"] < 1:
        raise ValueError("spot_probe_judge_min must be >= 1")
    if not isinstance(cfg["reuse_aliases"], bool):
        raise ValueError("reuse_aliases must be a boolean")
    return cfg


def open_phase(db, now):
    return db.execute(
        "SELECT * FROM experiment_phases WHERE closed_at IS NULL "
        "ORDER BY phase_id DESC LIMIT 1").fetchone()


def set_phase_config(db, **updates):
    """Validated config update on the open phase's config_json; the
    runner re-reads it each cycle so the change is live. Returns the
    new config, or None when no phase is open."""
    phase = open_phase(db, utcnow())
    if phase is None:
        return None
    cfg = validate_config(json.loads(phase["config_json"]))
    for key in updates:
        if key not in DEFAULT_CONFIG:
            raise ValueError(f"unknown config key {key!r}")
    cfg.update(updates)
    cfg = validate_config(cfg)
    with db:
        db.execute(
            "UPDATE experiment_phases SET config_json=? "
            "WHERE phase_id=?",
            (json.dumps(cfg, sort_keys=True), phase["phase_id"]))
    return cfg


def _live_cfg(db, phase, fallback):
    """Re-read the phase config each cycle so TUI/`set` toggles take
    effect without a restart; a malformed write keeps the last config
    rather than breaking the loop."""
    row = db.execute(
        "SELECT config_json FROM experiment_phases WHERE phase_id=?",
        (phase["phase_id"],)).fetchone()
    if row is None:
        return fallback
    try:
        return validate_config(json.loads(row["config_json"]))
    except ValueError:
        return fallback


def emit(db, ts, phase_id, order_id, server_id, kind, detail, say=print):
    """Append an experiment event and echo it as one stdout line."""
    detail_json = json.dumps(detail or {}, sort_keys=True)
    with db:
        db.execute(
            "INSERT INTO experiment_events"
            "(ts, phase_id, order_id, server_id, kind, detail_json)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (_iso(ts), phase_id, order_id, server_id, kind, detail_json))
    say(f"{_iso(ts)} {kind} order={order_id} server={server_id} "
        f"{detail_json}")


_REPEAT_MIN = 5


def _emit_throttled(db, now, phase_id, order_id, server_id, kind,
                    detail, sig_key, sig_val, say=print):
    """emit() for statuses that recur every cycle (blocked reasons,
    dry-run actions): skips when the same signature was logged within
    _REPEAT_MIN, so a stuck condition reads as a periodic 'still X'
    instead of a per-cycle flood."""
    row = db.execute(
        "SELECT ts FROM experiment_events WHERE phase_id=? AND kind=?"
        " AND order_id IS ? AND server_id IS ?"
        " AND json_extract(detail_json, ?) IS ?"
        " ORDER BY id DESC LIMIT 1",
        (phase_id, kind, order_id, server_id,
         f"$.{sig_key}", sig_val)).fetchone()
    if row is not None and \
            now - _dt(row["ts"]) < _minutes(_REPEAT_MIN):
        return
    emit(db, now, phase_id, order_id, server_id, kind, detail, say=say)


def _emit_blocked(db, now, phase_id, order_id, server_id, reason,
                  extra=None, say=print):
    """A `blocked` event at most once per _REPEAT_MIN per
    (target, reason) signature."""
    _emit_throttled(
        db, now, phase_id, order_id, server_id, "blocked",
        {"reason": reason, **(extra or {})},
        "reason", reason, say=say)


def share_arrivals(good_obs, prev0=None):
    """[(captured_at, count)] using shares.submitted as a cumulative
    counter that resets on miner restart: a rise counts the delta, a
    fall to >0 counts the full post-reset value, the first observation
    counts when >0. prev0 seeds the counter baseline (the last
    pre-resume reading) so a cumulative counter that survived an
    outbid pause is not read as one giant arrival at the first
    filtered observation."""
    arrivals = []
    prev = prev0 if isinstance(prev0, (int, float)) \
        and not isinstance(prev0, bool) else None
    for ts, status in good_obs:
        submitted = (status.get("shares") or {}).get("submitted")
        if not isinstance(submitted, (int, float)):
            continue
        if prev is None:
            if submitted > 0:
                arrivals.append((ts, submitted))
        elif submitted > prev:
            arrivals.append((ts, submitted - prev))
        elif submitted < prev and submitted > 0:
            arrivals.append((ts, submitted))
        prev = submitted
    return arrivals


def score_arrivals(good_obs, prev0=None):
    """[(captured_at, score_sum_delta)] on the cumulative
    shares.score_sum counter -- same reset semantics as
    share_arrivals. Solutions arrive already weighted by the
    network's per-ant ReportedScore, so these deltas are the rig's
    true weighted production where share arrivals are only the
    count. Empty until the pinned agent reports score fields."""
    arrivals = []
    prev = prev0 if isinstance(prev0, (int, float)) \
        and not isinstance(prev0, bool) else None
    for ts, status in good_obs:
        total = (status.get("shares") or {}).get("score_sum")
        if not isinstance(total, (int, float)):
            continue
        if prev is None:
            if total > 0:
                arrivals.append((ts, total))
        elif total > prev:
            arrivals.append((ts, total - prev))
        elif total < prev and total > 0:
            arrivals.append((ts, total))
        prev = total
    return arrivals


def stint_score_per_share(score_arr, share_arr):
    """Mean ReportedScore per submitted share over the arrival
    windows -- the rig's measured weight factor against the fleet
    mean. None when either side has no scored data."""
    score = sum(c for _, c in score_arr)
    shares = sum(c for _, c in share_arr)
    if not score or not shares:
        return None
    return score / shares


def _its_total(status):
    its = status.get("its") or {}
    if its.get("total") is not None:
        return its["total"]
    return (its.get("cpu") or 0) + (its.get("gpu") or 0)


def _observations(db, order_id):
    good = []
    for row in db.execute(
            "SELECT captured_at, status_json FROM rig_observations "
            "WHERE order_id=? ORDER BY id", (order_id,)):
        if row["status_json"] is None:
            continue
        try:
            status = json.loads(row["status_json"])
        except ValueError:
            continue
        good.append((_dt(row["captured_at"]), status))
    return good


def _restart_events(db, phase_id, order_id):
    return [_dt(row["ts"]) for row in db.execute(
        "SELECT ts FROM experiment_events WHERE phase_id=? AND "
        "order_id=? AND kind='restart' ORDER BY id",
        (phase_id, order_id))]


def _loads_ok(raw):
    try:
        json.loads(raw)
        return True
    except ValueError:
        return False


def _row_mode(row):
    """experiment_rentals.mode, defaulting for databases written
    before the column existed (read paths never run the ALTER)."""
    try:
        return row["mode"]
    except (IndexError, KeyError):
        return "on-demand"


def _row_opt(row, key):
    """Nullable migrated column, None on a pre-migration schema."""
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


def _rental_state(db, phase, row, now, cfg, listings, paused_ids=None):
    order_id = row["order_id"]
    # a reactivated (resumed) spot order re-baselines everything that
    # derives from observations: the hours spent outbid are not a dry
    # spell on this attempt
    resumed = _dt(_row_opt(row, "resumed_at"))
    fleet = db.execute(
        "SELECT * FROM fleet_rentals WHERE order_id=?",
        (order_id,)).fetchone()
    all_rows = [(_dt(r["captured_at"]), r["status_json"],
                 r["error_kind"])
                for r in db.execute(
                    "SELECT captured_at, status_json, error_kind FROM "
                    "rig_observations WHERE order_id=? ORDER BY id",
                    (order_id,))]
    prev0 = prev0_score = None
    if resumed is not None:
        # seed the share baseline at the last good pre-resume reading:
        # a cumulative counter that survived the pause would otherwise
        # fabricate its whole history as a single arrival at t0 (a
        # reset counter still lands in the fall-to->0 reset rule)
        for ts, raw, _err in reversed(all_rows):
            if ts >= resumed or raw is None or not _loads_ok(raw):
                continue
            sh = json.loads(raw).get("shares") or {}
            submitted, score_sum = sh.get("submitted"), \
                sh.get("score_sum")
            if isinstance(submitted, (int, float)) \
                    and not isinstance(submitted, bool):
                prev0 = submitted
            if isinstance(score_sum, (int, float)) \
                    and not isinstance(score_sum, bool):
                prev0_score = score_sum
            break
        all_rows = [(ts, raw, err) for ts, raw, err in all_rows
                    if ts >= resumed]
    good = [(ts, json.loads(raw)) for ts, raw, _err in all_rows
            if raw is not None and _loads_ok(raw)]
    unreachable_window = now - _minutes(cfg["unreachable_min"])
    window_rows = [(raw, err) for ts, raw, err in all_rows
                   if ts >= unreachable_window]
    last_good_at_tmp = good[-1][0] if good else None
    created = resumed or _dt(row["created_at"])
    recent_errors_only = bool(window_rows) and all(
        raw is None for raw, _err in window_rows) and (
        last_good_at_tmp is not None
        and now - last_good_at_tmp >= _minutes(cfg["unreachable_min"])
        or last_good_at_tmp is None
        and now - created >= _minutes(cfg["unreachable_min"]))
    unreach = None
    if recent_errors_only:
        err_kinds = {}
        for _raw, err in window_rows:
            if err:
                err_kinds[err] = err_kinds.get(err, 0) + 1
        unreach = {
            "n_obs": len(window_rows),
            "errors": err_kinds,
            "never_reached": last_good_at_tmp is None,
            "last_good_at": (_iso(last_good_at_tmp)
                             if last_good_at_tmp else None),
            "silent_min": int(
                (now - (last_good_at_tmp or created))
                .total_seconds() // 60),
        }
    t0 = good[0][0] if good else None
    last_good_at = good[-1][0] if good else None
    last_status = good[-1][1] if good else {}
    its_last = _its_total(last_status) if good else None
    its_total = its_last
    if good:
        # smoothed it/s: median of the trailing window, skipping the
        # first 2 min after t0 while the miner warms up; fall back to
        # the whole window, then to the last sample
        cut = now - _minutes(cfg["its_window_min"])
        windowed = [(ts, s) for ts, s in good if ts >= cut]
        post_warm = [s for ts, s in windowed
                     if ts >= t0 + _minutes(2)]
        samples = post_warm or [s for _, s in windowed]
        if samples:
            its_total = _median([_its_total(s) for s in samples])
    arrivals = share_arrivals(good, prev0)
    score_arr = score_arrivals(good, prev0_score)
    score_per_share = stint_score_per_share(score_arr, arrivals)
    last_share_at = arrivals[-1][0] if arrivals else None
    restarts = [ts for ts in _restart_events(
        db, phase["phase_id"], order_id)
        if resumed is None or ts >= resumed]
    quiet_since = last_share_at or t0
    reroll_start = None
    if quiet_since is not None:
        after = [ts for ts in restarts if ts >= quiet_since]
        reroll_start = after[0] if after else None
    listing = listings.get(row["server_id"])
    gpu_count = (len(listing.get("gpu_array") or [])
                 if listing is not None else None)
    hardware = last_status.get("hardware") or {}
    deadline = _dt(fleet["deadline_at"]) if fleet else None
    # trainer version transitions across the observation history:
    # each bump is candidate evidence of an algo/economics change,
    # emitted as `trainer` events so version shifts can be
    # correlated against share-rate moves after the fact
    trainer_seq = []
    for ts, status in good:
        t = status.get("trainer") or {}
        label = " ".join(x for x in (
            t.get("variant") or t.get("flavor"),
            f"v{t['version']}" if t.get("version") else None) if x)
        if label and (not trainer_seq or trainer_seq[-1][1] != label):
            trainer_seq.append((ts, label))
    trainer_changes = [
        {"at": new[0], "from": old[1], "to": new[1]}
        for old, new in zip(trainer_seq, trainer_seq[1:])]
    return {
        "order_id": order_id,
        "server_id": row["server_id"],
        "tier": row["tier"],
        "reason": row["reason"],
        "role": row["role"],
        "burst_id": row["burst_id"],
        "x_min": row["x_min"],
        "y_min": row["y_min"],
        "mode": _row_mode(row),
        "resumed_at": resumed,
        "paused": bool(paused_ids) and order_id in paused_ids,
        "hourly_usd": _dec(row["hourly_usd"]),
        "creation_fee_usd": _dec(row["creation_fee_usd"]),
        "quoted_cost_usd": _dec(row["quoted_cost_usd"]),
        "created_at": created,
        "age": now - created,
        "deadline_at": deadline,
        "provision_state": fleet["provision_state"] if fleet else None,
        "auto_renew": bool(fleet["auto_renew"]) if fleet else False,
        "ssh_host": fleet["ssh_host"] if fleet else None,
        "ssh_port": fleet["ssh_port"] if fleet else None,
        "ssh_key_path": fleet["ssh_key_path"] if fleet else None,
        "t0": t0,
        "last_good_at": last_good_at,
        "obs_age": (now - last_good_at) if last_good_at else None,
        "last_two_zero_its": len(good) >= 2 and all(
            _its_total(status) == 0 for _, status in good[-2:]),
        "its_total": its_total,
        "its_last": its_last,
        "gpu_its": (last_status.get("its") or {}).get("gpu"),
        "gpu_count_seen": (len(hardware["gpus"])
                           if isinstance(hardware.get("gpus"), list)
                           else None),
        "listing_gpu_count": gpu_count,
        "recent_errors_only": recent_errors_only,
        "unreach": unreach,
        "last_obs_at": all_rows[-1][0] if all_rows else None,
        "last_share_at": last_share_at,
        "share_arrivals": arrivals,
        "score_arrivals": score_arr,
        "score_per_share": score_per_share,
        "score_med": (last_status.get("shares") or {}).get("score_med"),
        "last_restart_at": restarts[-1] if restarts else None,
        "restarts": restarts,
        "reroll_start": reroll_start,
        "quiet_since": quiet_since,
        "good_obs": good,
        "spec_mismatch": spec_mismatch(listing, last_status),
        "reused_from": _row_opt(row, "reused_from"),
        "trainer": trainer_seq[-1][1] if trainer_seq else None,
        "trainer_changes": trainer_changes,
    }


# fleet_rentals.provision_state values where no worker can still be
# connected under the alias -- used only when no fresh order
# inventory exists to prove the order's absence.
_TERMINAL_FLEET_STATES = ("cancelled", "missing", "provision_failed")


def alias_donor(db, now, cfg, inventory=None):
    """A reusable worker identity: the alias of the most recent ended
    rental that is provably dead.

    Measured 2026-11-17 (0/3 recipients): the pool does not restore a
    dead worker's tree pin, so reuse buys dashboard hygiene -- a
    bounded alias count on the pool's worker page -- not tree luck.
    Any provably-dead identity qualifies. 'Provably dead' means:

    - no open rental or pending rent attempt still holds the alias;
    - the order is gone: absent from a fresh orders inventory, or
      (with no fresh inventory) ended by us with a terminal fleet
      state. An 'outbid' spot order's standing bid can reactivate it,
      so without fresh inventory proving it gone its alias stays
      off-limits.

    Returns the newest eligible donor dict or None."""
    inventory = inventory if inventory is not None \
        else orders.read_inventory(db)
    live_ids = orders.fresh_order_ids(inventory, now)
    pending_aliases = {p["alias"] for p in fleet_store
                       .pending_attempts(db)}
    open_aliases = {r["alias"] for r in db.execute(
        "SELECT f.alias FROM fleet_rentals f JOIN experiment_rentals e"
        " ON e.order_id=f.order_id WHERE e.ended_at IS NULL"
        " UNION SELECT alias FROM fleet_rentals WHERE"
        " provision_state IN ('pending','ready')")}
    for row in db.execute(
            "SELECT e.order_id, e.server_id, e.ended_at, e.end_reason,"
            " f.alias, f.provision_state"
            " FROM experiment_rentals e"
            " JOIN fleet_rentals f ON f.order_id=e.order_id"
            " WHERE e.ended_at IS NOT NULL AND e.ended_at<=?"
            " ORDER BY e.ended_at DESC LIMIT 60",
            (_iso(now),)).fetchall():
        ended = _dt(row["ended_at"])
        if ended is None:
            continue
        alias = row["alias"]
        if not alias or alias in open_aliases \
                or alias in pending_aliases:
            continue
        if live_ids is not None:
            if row["order_id"] in live_ids:
                # still listed -- live or paused/resumable
                continue
        elif row["end_reason"] == "outbid":
            # a spot bid survives its order; with no fresh inventory
            # to prove it gone the identity is not provably dead
            continue
        elif row["provision_state"] not in _TERMINAL_FLEET_STATES:
            continue
        return {"order_id": row["order_id"],
                "server_id": row["server_id"], "alias": alias,
                "ended_at": ended}
    return None


def pending_rerents(db, phase_id):
    """rerent_scheduled events with no later rent/rerent_expired for
    the same server in this phase, as {server_id, due_at, tier}."""
    pending = {}
    for row in db.execute(
            "SELECT kind, server_id, detail_json, ts FROM "
            "experiment_events WHERE phase_id=? ORDER BY id",
            (phase_id,)):
        sid = row["server_id"]
        if row["kind"] == "rerent_scheduled":
            detail = json.loads(row["detail_json"])
            pending[sid] = {"server_id": sid,
                            "due_at": _dt(detail["due_at"]),
                            "tier": detail.get("tier") or "other"}
        elif sid in pending and row["kind"] in (
                "rent", "rent_failed", "rerent_expired"):
            del pending[sid]
    return list(pending.values())


def pending_bursts(db, phase_id):
    """burst_scheduled events with no later burst_started for the same
    for_order (or a retry_expired/retry_cleared), as pending retry
    bursts for vacated slots."""
    pending = {}
    for row in db.execute(
            "SELECT kind, order_id, detail_json FROM experiment_events "
            "WHERE phase_id=? ORDER BY id", (phase_id,)):
        detail = json.loads(row["detail_json"])
        if row["kind"] == "burst_scheduled":
            key = detail["for_order"]
            pending[key] = {"for_order": key,
                            "server_id": detail.get("server_id"),
                            "tier": detail.get("tier"),
                            "y_min": detail["y_min"],
                            "proven": detail.get("proven"),
                            "due_at": _dt(detail["due_at"])}
        elif row["kind"] == "burst_started" \
                and row["order_id"] is not None:
            pending.pop(row["order_id"], None)
        elif row["kind"] in ("retry_expired", "retry_cleared"):
            pending.pop(detail.get("for_order"), None)
    return list(pending.values())


def probe_pins(db, phase_id):
    """Operator-tagged servers auditioned first on the next probe fill,
    in tag order. Consumed when the server rents in any role."""
    return [r["server_id"] for r in db.execute(
        "SELECT server_id FROM probe_pins WHERE phase_id=? "
        "ORDER BY tagged_at, server_id", (phase_id,))]


def set_probe_pin(db, phase_id, server_id, on):
    with db:
        if on:
            db.execute(
                "INSERT OR REPLACE INTO probe_pins(phase_id, "
                "server_id, tagged_at) VALUES (?, ?, ?)",
                (phase_id, int(server_id), _iso(utcnow())))
        else:
            db.execute(
                "DELETE FROM probe_pins WHERE phase_id=? "
                "AND server_id=?", (phase_id, int(server_id)))


# rent pipeline failures (detail.info.stage) that are transient
# infra/API bounces: no order was placed and the server did nothing
# wrong. They cool down under quote_rejected like quote-stage
# failures instead of exiling the listing for the phase.
TRANSIENT_RENT_STAGES = frozenset({
    "lock", "secrets", "refetch", "guards", "spot_recheck",
    "attempt", "post", "reconcile"})


def excluded_servers(db, phase_id):
    """Servers cancelled for fault/unreachable or whose rent failed
    at a server-fault stage (register/provision/ready: the rig took
    an order and failed to deliver); never re-chosen this phase.
    Quote and transient pipeline failures are not faults -- they get
    the shorter quote_reject_min cooldown instead. Unknown stages
    stay excluded: an unclassified failure errs toward not retrying."""
    excluded = set()
    for row in db.execute(
            "SELECT server_id, end_reason FROM experiment_rentals "
            "WHERE phase_id=? AND ended_at IS NOT NULL", (phase_id,)):
        reason = row["end_reason"] or ""
        if reason.startswith("fault:") or reason == "unreachable":
            excluded.add(row["server_id"])
    for row in db.execute(
            "SELECT server_id, detail_json FROM experiment_events"
            " WHERE phase_id=? AND kind='rent_failed'"
            " AND server_id IS NOT NULL", (phase_id,)):
        try:
            detail = json.loads(row["detail_json"])
        except (ValueError, TypeError):
            detail = {}
        stage = ((detail.get("info") or {}).get("stage")
                 or detail.get("stage"))
        if stage != "quote" and stage not in TRANSIENT_RENT_STAGES:
            excluded.add(row["server_id"])
    return excluded


# owner fault accounting: a host that keeps delivering dead boxes
# burns probe budget across its whole fleet -- per-server exclusions
# can't see the pattern because every rental is a different listing.
BAD_OWNER_MIN_FAULTS = 3
BAD_OWNER_FAULT_RATE = 0.4


def bad_owners(db, scored):
    """Owners with >=BAD_OWNER_MIN_FAULTS fault endings at a
    >BAD_OWNER_FAULT_RATE share of their rentals. A dead box is the
    host's fault, not the listing's -- owner 54202 shipped six dead
    rigs across ten servers before this existed."""
    faults, totals = {}, {}
    for row in db.execute(
            "SELECT server_id, end_reason FROM experiment_rentals"
            " WHERE ended_at IS NOT NULL"):
        row_scored = scored.get(row["server_id"])
        owner = row_scored.get("owner") if row_scored else None
        if owner is None:
            continue
        totals[owner] = totals.get(owner, 0) + 1
        if str(row["end_reason"] or "").startswith("fault:"):
            faults[owner] = faults.get(owner, 0) + 1
    return {o for o, n in totals.items()
            if faults.get(o, 0) >= BAD_OWNER_MIN_FAULTS
            and faults[o] / n > BAD_OWNER_FAULT_RATE}


def latest_listings(db):
    """Latest marketplace snapshot: {server_id: listing} plus the
    snapshot's captured_at."""
    row = db.execute(
        "SELECT captured_at, raw_json FROM marketplace_snapshots "
        "ORDER BY id DESC LIMIT 1").fetchone()
    if row is None or not row["raw_json"]:
        return {}, None
    try:
        servers = json.loads(row["raw_json"]).get("servers") or []
    except ValueError:
        return {}, _dt(row["captured_at"])
    return {int(s["id"]): s for s in servers if s.get("id") is not None}, \
        _dt(row["captured_at"])


def server_listings(db):
    """servers.latest_json per server: the newest known listing."""
    result = {}
    if not db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='servers'").fetchone():
        return result
    for row in db.execute("SELECT server_id, latest_json FROM servers"):
        try:
            result[row["server_id"]] = json.loads(row["latest_json"])
        except (ValueError, TypeError):
            pass
    return result


def history_stats(db, known_listings, exclude_order_id=None, now=None):
    """Per-hardware history from all past fleet_rentals:
    coverage {(cpu, gpu): observed hours},
    cpu_prior {cpu: median its.cpu after the rental's first third},
    gpu_prior {gpu: median its.gpu ditto}.
    Priors prefer current-epoch observations; prior-epoch samples
    only fill keys with no same-epoch data and are flagged in
    cross_epoch -- a pool algorithm change makes old numbers a
    different unit, useful for ordering but never trusted blindly."""
    now = now or utcnow()
    cur_ep = _epoch_idx(now)
    coverage = {}
    cpu_samples = {"cur": {}, "old": {}}
    gpu_samples = {"cur": {}, "old": {}}
    server_samples = {"cur": {}, "old": {}}
    rental_counts = {"cpu": {}, "gpu": {}}
    epoch_share_totals = {}
    # payout weight per GPU class: pool-weighted sol per raw submitted
    # share. Worker sol is per-session cumulative and resets when the
    # alias reconnects, so a stint's earnings are the sum of sol peaks
    # between in-window drops; the window ends when the next rental
    # claims the alias (an idle alias keeps reporting its final sol).
    sol_series = {}
    try:
        for row in db.execute(
                "SELECT w.alias, s.source_ts, w.sol"
                " FROM pool_worker_snapshots w"
                " JOIN pool_snapshots s ON s.id=w.snapshot_id"
                " ORDER BY w.alias, s.source_ts"):
            sol_series.setdefault(row["alias"], []).append(
                (_dt(row["source_ts"]), row["sol"] or 0.0))
    except sqlite3.OperationalError:
        pass
    alias_next = {}
    for row in db.execute(
            "SELECT alias, created_at FROM fleet_rentals"
            " ORDER BY created_at"):
        alias_next.setdefault(row["alias"], []).append(row["created_at"])
    weight_samples = {}
    # mean ReportedScore per submitted share, per stint: the rig's
    # measured payout weight. score_ref below is the fleet median of
    # these -- the normalizer for score-weighted judging.
    score_samples = {}
    for fleet in db.execute("SELECT * FROM fleet_rentals"):
        if fleet["order_id"] == exclude_order_id:
            continue
        listing = known_listings.get(fleet["server_id"])
        if listing is None:
            continue
        cpu = (listing.get("specs") or {}).get("cpu") or "Unknown CPU"
        gpu = market.offered_gpu(listing)
        key = (cpu, gpu)
        good = _observations(db, fleet["order_id"])
        if not good:
            continue
        t0, last = good[0][0], good[-1][0]
        coverage[key] = coverage.get(key, 0.0) + \
            max(0.0, (last - t0).total_seconds() / 3600.0)
        cutoff = t0 + (last - t0) / 3
        samples = {"cpu": {"cur": [], "old": []},
                   "gpu": {"cur": [], "old": []}}
        for ts, status in good:
            if ts < cutoff:
                continue
            ep = "cur" if _epoch_idx(ts) == cur_ep else "old"
            its = status.get("its") or {}
            for device in samples:
                value = its.get(device)
                if isinstance(value, (int, float)) and not isinstance(
                        value, bool) and math.isfinite(value) and value >= 0:
                    samples[device][ep].append(value)
        # a rig that produced no shares proved nothing end-to-end:
        # the miner may hash fine yet earn nothing (rejected work,
        # tree misses). Priors only count share-reporting rentals.
        def _found(st):
            return max((st.get("shares") or {}).get(k) or 0
                       for k in ("found", "log_found", "submitted",
                                 "log_submitted"))
        cur_found = max((_found(st) for ts, st in good
                         if _epoch_idx(ts) == cur_ep), default=0)
        pre_found = max((_found(st) for ts, st in good
                         if _epoch_idx(ts) != cur_ep), default=0)
        shared = max(cur_found, pre_found) > 0
        if gpu is not None:
            # cumulative counter: this epoch's shares are the cur-epoch
            # peak minus whatever the rental brought in with it
            epoch_share_totals[gpu] = epoch_share_totals.get(gpu, 0) \
                + max(0, cur_found - pre_found)
            # stint payout weight: raw submitted shares (miner counter)
            # against summed sol peaks in the stint's window. A sol
            # drop inside the window is a mid-stint restart boundary.
            end_cap = None
            for nxt in alias_next.get(fleet["alias"], []):
                nd = _dt(nxt)
                if nd > _dt(fleet["created_at"]):
                    end_cap = nd
                    break
            sh_arr = share_arrivals(good)
            submitted_total = sum(c for _, c in sh_arr)
            sps = stint_score_per_share(
                score_arrivals(good), sh_arr)
            if sps is not None and shared:
                score_samples.setdefault(gpu, []).append(sps)
            sols = [(ts_s, sol) for ts_s, sol
                    in sol_series.get(fleet["alias"], [])
                    if ts_s >= _dt(fleet["created_at"])
                    and (end_cap is None or ts_s < end_cap)]
            if sols and submitted_total > 0:
                if len(sols) > 1 and sols[0][1] > sols[1][1]:
                    sols = sols[1:]
                seg, prev, sol_earned = 0.0, None, 0.0
                for _, sol in sols:
                    if prev is not None and sol < prev:
                        sol_earned += seg
                        seg = 0.0
                    seg = max(seg, sol)
                    prev = sol
                sol_earned += seg
                weight_samples.setdefault(gpu, []).append(
                    (sol_earned, submitted_total))
        gpu_n = len(listing.get("gpu_array") or []) or 1
        for device, key, target in (
                ("cpu", cpu, cpu_samples), ("gpu", gpu, gpu_samples)):
            if key is None:
                continue
            if samples[device]["cur"] or samples[device]["old"]:
                counts = rental_counts[device]
                counts[key] = counts.get(key, 0) + 1
            for ep in ("cur", "old"):
                vals = samples[device][ep]
                if device == "gpu" and gpu_n > 1:
                    # priors are per-GPU: a 2-card rig's measured gpu
                    # its is normalized so it never inflates the
                    # single-card estimate for the same model
                    vals = [v / gpu_n for v in vals]
                med = _median(vals)
                if med is not None:
                    # one median per rental: obs-pooling let a single
                    # long-running or long-dead rental dominate the
                    # prior for the whole hardware class
                    target[ep].setdefault(key, []).append(
                        (med, shared))
        for ep in ("cur", "old"):
            per_rental = sum(
                _median(samples[d][ep]) or 0 for d in ("cpu", "gpu"))
            if per_rental > 0:
                server_samples[ep].setdefault(
                    fleet["server_id"], []).append(
                        (per_rental, shared))

    # a dead-median rental is host-fault evidence, not hardware
    # capability. Starved containers still report 5-25 it/s, so the
    # floor is relative: below 30% of the pooled median = dead box.
    DEAD_MEDIAN = 5.0
    DEAD_FRAC = 0.3
    # the current epoch's samples only displace older evidence once
    # enough rentals support them -- one faulted probe must not
    # override a dozen healthy rentals from the previous epoch
    PRIOR_MIN_RENTALS = 3

    def _healthy(cur, old):
        pooled = _median(cur + old) or 0
        floor = max(DEAD_MEDIAN, DEAD_FRAC * pooled)
        return ([m for m in cur if m >= floor],
                [m for m in old if m >= floor])

    def _prior(targets, ce_set, detail=None):
        out = {}
        for key in set(targets["cur"]) | set(targets["old"]):
            cur, old = _healthy(
                [m for m, sh in targets["cur"].get(key, []) if sh],
                [m for m, sh in targets["old"].get(key, []) if sh])
            cross = False
            if len(cur) >= PRIOR_MIN_RENTALS:
                out[key] = _median(cur)
            elif cur:
                out[key] = _median(cur + old)
                cross = bool(old)
            elif old:
                out[key] = _median(old)
                cross = True
            if cross:
                ce_set.add(key)
            if detail is not None:
                ws = weight_samples.get(key, [])
                sol_tot = sum(s for s, _ in ws)
                sub_tot = sum(su for _, su in ws)
                sps_list = score_samples.get(key, [])
                detail[key] = {
                    "prior": out.get(key),
                    "proven": len(cur) + len(old),
                    "rentals": len(targets["cur"].get(key, []))
                               + len(targets["old"].get(key, [])),
                    "shares": epoch_share_totals.get(key, 0),
                    "weight": (sol_tot / sub_tot) if sub_tot else None,
                    "weight_n": len(ws),
                    "sol": round(sol_tot, 1),
                    "score_per_share": (_median(sps_list)
                                        if sps_list else None),
                    "score_n": len(sps_list),
                    "cross_epoch": cross}
        return out

    cpu_ce, gpu_ce, srv_ce = set(), set(), set()
    gpu_detail = {}
    server_prior = {}
    for sid in set(server_samples["cur"]) | set(server_samples["old"]):
        cur, old = _healthy(
            [m for m, sh in server_samples["cur"].get(sid, []) if sh],
            [m for m, sh in server_samples["old"].get(sid, [])
             if sh])
        if len(cur) >= PRIOR_MIN_RENTALS:
            v, ce = cur, False
        elif cur:
            v, ce = cur + old, bool(old)
        elif old:
            v, ce = old, True
        else:
            continue
        server_prior[sid] = {"its": _median(v), "rentals": len(v),
                             "cross_epoch": ce}
        if ce:
            srv_ce.add(sid)
    return {
        "coverage": coverage,
        "cpu_prior": _prior(cpu_samples, cpu_ce),
        "gpu_prior": _prior(gpu_samples, gpu_ce, gpu_detail),
        "gpu_detail": gpu_detail,
        "cpu_rentals": rental_counts["cpu"],
        "gpu_rentals": rental_counts["gpu"],
        # per-server hardware history: median total it/s across this
        # server's past rentals. it/s is hardware-bound (tree luck only
        # moves share yield), so this survives a fresh tree draw.
        "server_prior": server_prior,
        # fleet-wide mean ReportedScore per share across every
        # score-reporting stint -- the normalizer for score-weighted
        # yield. First stints read ~3450-3500; a class below that is
        # submitting lower-quality ants.
        "score_ref": _median(
            [s for v in score_samples.values() for s in v]),
        # keys whose prior came from a previous epoch: estimate with
        # suspicion -- the pool algorithm may have moved the unit.
        "cross_epoch": {"cpu": cpu_ce, "gpu": gpu_ce,
                        "server": srv_ce},
    }


def pool_share_estimate(db, rentals, cfg, now=None, stats=None,
                        scored=None, remaining=None):
    """Budget estimate for holding cfg['pool_share_target_pct']% of
    the pool's window presence (total_sol).

    Chain: needed_sol = target x total_sol;
    needed_its = needed_sol / sol-per-its (ours measured, pool's as
    fallback); est_cost_hr = supply-curve price of needed_its under
    pool_share_max_price_hr (measured fleet eff as fallback).

    Returns None when no pool snapshot is available. Fields stay
    honest about which inputs are measured vs fallbacks.
    """
    row = db.execute(
        "SELECT source_ts, extra_json FROM pool_snapshots"
        " ORDER BY source_ts DESC LIMIT 1").fetchone()
    if not row:
        return None
    ext = json.loads(row["extra_json"] or "{}")
    total_sol, total_its = ext.get("pool_total_sol"), \
        ext.get("pool_total_its")
    workers = db.execute(
        "SELECT COALESCE(SUM(w.sol),0), COALESCE(SUM(w.its),0)"
        " FROM pool_worker_snapshots w JOIN pool_snapshots s"
        "   ON s.id=w.snapshot_id WHERE s.source_ts=?",
        (row["source_ts"],)).fetchone()
    our_sol, our_its = workers[0] or 0.0, workers[1] or 0.0

    # measured sol per its; fall back to the pool-wide ratio
    if our_its > 0 and our_sol > 0:
        sol_per_its = our_sol / our_its
    elif total_its:
        sol_per_its = (total_sol or 0) / total_its
    else:
        sol_per_its = None

    # fleet-measured its per $/hr across share-proven stints;
    # candidates' est_its/price is the fallback, a bare None last
    effs = [r["its_total"] / float(r["hourly_usd"])
            for r in rentals
            if r.get("its_total") and float(r["hourly_usd"] or 0) > 0
            and sum(c for _, c in r.get("share_arrivals", [])) > 0]
    eff = _median(effs)

    # the estimate always computes -- enabled only gates arming
    # (approval, warnings, catch-up pricing), not visibility
    enabled = bool(cfg.get("pool_share_enabled"))
    target_pct = float(cfg.get("pool_share_target_pct") or 0)
    needed_sol = (target_pct / 100.0) * total_sol \
        if total_sol else None
    needed_its = (needed_sol / sol_per_its
                  if needed_sol is not None and sol_per_its else None)

    # fleet capacity for the deficit trigger: pool-feed its vs the
    # sum of measured rental its -- the max covers fresh rigs whose
    # workers haven't appeared in the feed yet, so a just-placed
    # catch-up order already counts toward the deficit
    fleet_its = max(our_its or 0.0, sum(
        float(r["its_total"]) for r in rentals if r.get("its_total")))
    deficit_its = (max(0.0, needed_its - fleet_its)
                   if needed_its is not None else None)

    # its supply under the share price cap, best eff first; the walk
    # prices the target off what is actually buyable and reports the
    # ceiling when even the whole under-cap market falls short
    cap = cfg.get("pool_share_max_price_hr") or cfg["max_price_hr"]
    supply = _share_supply(scored, stats, cap)
    cap_ceiling_its = (sum(r[1] for r in supply)
                       if supply else None)
    est_cost_hr = None
    supply_short_its = None
    cost_basis = None
    if needed_its:
        got, cost = _walk_share_supply(supply, need_its=needed_its)
        if got >= needed_its:
            est_cost_hr, cost_basis = cost, "supply_curve"
        else:
            supply_short_its = needed_its - got
            if eff:  # soft fallback: fleet-measured efficiency
                est_cost_hr = needed_its / eff
                cost_basis = "measured_eff"
    elif needed_its is not None:
        est_cost_hr, cost_basis = 0.0, "supply_curve"

    epoch_h_left = None
    prog = ext.get("epoch_progress")
    if isinstance(prog, str) and prog.endswith("%"):
        try:
            epoch_h_left = max(
                0.0, (1.0 - float(prog[:-1]) / 100.0) * 168.0)
        except ValueError:
            pass
    est_cost_epoch = (est_cost_hr * epoch_h_left
                      if est_cost_hr and epoch_h_left else None)

    # what the remaining budget still buys at the current curve
    affordable_pct = None
    if remaining is not None and epoch_h_left \
            and sol_per_its and total_sol:
        hr_budget = max(0.0, float(remaining) / epoch_h_left)
        a_its, _ = _walk_share_supply(supply, budget_hr=hr_budget)
        affordable_pct = 100.0 * a_its * sol_per_its / total_sol

    approved = {
        "usd": float(cfg.get("pool_share_approved_usd") or 0),
        "hr": float(cfg.get("pool_share_approved_hr") or 0),
        "pct": float(cfg.get("pool_share_approved_pct") or 0),
        "at": cfg.get("pool_share_approved_at") or None,
    }
    cost_drift = (est_cost_hr / approved["hr"] - 1.0
                  if approved["hr"] > 0 and est_cost_hr else None)
    reapproval_needed = bool(
        enabled and approved["usd"] > 0 and (
            approved["pct"] != target_pct
            or (cost_drift is not None
                and abs(cost_drift)
                > cfg["pool_share_reapprove_drift"])))
    pace_deficit = bool(
        enabled and est_cost_epoch is not None
        and remaining is not None
        and float(remaining) < est_cost_epoch)

    return {
        "enabled": enabled,
        "target_pct": target_pct,
        "share_now_pct": (100.0 * our_sol / total_sol
                          if total_sol else None),
        "our_sol": our_sol, "our_its": our_its,
        "total_sol": total_sol, "total_its": total_its,
        "pool_miners": ext.get("pool_miners"),
        "sol_per_its": sol_per_its,
        "eff_its_per_usd": eff,
        "eff_n": len(effs),
        "needed_sol": needed_sol, "needed_its": needed_its,
        "fleet_its": fleet_its,
        "deficit_its": deficit_its,
        "est_cost_hr": est_cost_hr,
        "est_cost_day": (est_cost_hr * 24 if est_cost_hr else None),
        "est_cost_epoch": est_cost_epoch,
        "cost_basis": cost_basis,
        "cap_ceiling_its": cap_ceiling_its,
        "supply_short_its": supply_short_its,
        "affordable_pct": affordable_pct,
        "approved": approved,
        "cost_drift": cost_drift,
        "reapproval_needed": reapproval_needed,
        "pace_deficit": pace_deficit,
        "epoch_h_left": epoch_h_left,
        "snapshot_ts": row["source_ts"],
        "snapshot_age_s": ((now - _dt(row["source_ts"])).total_seconds()
                           if now else None),
    }


def _share_supply(scored, stats, price_cap):
    """Rentable its supply under `price_cap`, best its-per-$ first.
    Rows are (eff, its, hourly_usd) over listings whose price and
    est_its are both known and not flagged ineligible."""
    rows = []
    cap = float(_dec(price_cap))
    for sid, row in (scored or {}).items():
        hourly = row.get("hourly_cost_usd")
        if hourly is None or float(hourly) > cap:
            continue
        # same tolerance _candidates gives: "no matching benchmark"
        # rigs still estimate from model-class priors
        if row.get("ineligible_reason") not in \
                (None, "no matching benchmark"):
            continue
        its, _, _ = est_its(
            stats, sid, row.get("cpu"), row.get("gpu"),
            gpu_count=row.get("gpu_count") or 1)
        if not its:
            continue
        rows.append((float(its) / float(hourly), float(its),
                     float(hourly)))
    rows.sort(key=lambda r: -r[0])
    return rows


def _walk_share_supply(rows, need_its=None, budget_hr=None):
    """Consume the supply rows best-first until `need_its` is covered
    or `budget_hr` is spent; returns (its_acquired, cost_hr)."""
    its = cost = 0.0
    for _, r_its, r_hr in rows:
        if need_its is not None and its >= need_its:
            break
        if budget_hr is not None and cost + r_hr > budget_hr:
            continue  # too dear for the remaining budget; try cheaper
        its += r_its
        cost += r_hr
    return its, cost


def approve_pool_share(db, now=None):
    """Snapshot the current share-target estimate as the funded
    objective: records the approved cost/pct in the phase config and
    sets budget_usd = committed + est_cost_epoch, so `remaining`
    exactly funds the forward estimate. Returns the new estimate
    (with approval fields) or raises ValueError when there is
    nothing priceable to approve."""
    now = now or utcnow()
    phase = open_phase(db, now)
    if phase is None:
        raise ValueError("no open phase")
    cfg = validate_config(json.loads(phase["config_json"]))
    state = build_state(db, dict(phase), now, cfg,
                        apply_endings=False, say=lambda *a: None)
    est = state["pool_share"] or {}
    if est.get("est_cost_epoch") is None:
        raise ValueError(
            "no epoch-cost estimate yet (need pool totals and a "
            "reachable supply curve)")
    set_phase_config(
        db,
        pool_share_approved_usd=est["est_cost_epoch"],
        pool_share_approved_hr=est["est_cost_hr"] or 0.0,
        pool_share_approved_pct=est["target_pct"],
        pool_share_approved_at=_iso(now))
    # ceiling to cents: the funded budget must never land below the
    # estimate it approved (rounding down would under-fund by <1c)
    new_budget = (state["committed"] + _dec(str(est["est_cost_epoch"]))
                  ).quantize(Decimal("0.01"), rounding=ROUND_CEILING)
    with db:
        db.execute(
            "UPDATE experiment_phases SET budget_usd=? WHERE phase_id=?",
            (f"{new_budget:.2f}", phase["phase_id"]))
    return {"budget_usd": f"{new_budget:.2f}",
            "committed": f"{state['committed']:.2f}",
            "approved": est["target_pct"],
            "est_cost_epoch": est["est_cost_epoch"],
            "est_cost_hr": est["est_cost_hr"]}


def _median(values):
    values = sorted(values)
    if not values:
        return None
    mid = len(values) // 2
    if len(values) % 2:
        return values[mid]
    return (values[mid - 1] + values[mid]) / 2


def _gpu_est(stats, gpu):
    """Measured prior if the model was rented, else extrapolated from
    the measured models via GPU_IT_SCALE relative multipliers.
    Returns (its, extrapolated, cross_epoch)."""
    ce = (stats.get("cross_epoch") or {}).get("gpu") or ()
    prior = stats["gpu_prior"].get(gpu)
    if prior is not None:
        return prior, False, gpu in ce
    scale = market.GPU_IT_SCALE.get(gpu)
    if scale is None:
        return None, False, False
    bases = [(g, p / market.GPU_IT_SCALE[g])
             for g, p in stats["gpu_prior"].items()
             if g in market.GPU_IT_SCALE]
    if not bases:
        return None, False, False
    base = sorted(p for _, p in bases)[len(bases) // 2]
    cross = any(g in ce for g, _ in bases)
    return base * scale, True, cross


def _cpu_est(stats, cpu):
    """Measured prior if the CPU was rented, else a measured sibling
    model's prior (X3D cache variants mine identically), else the
    median prior of its ISA class (AVX-512 vs AVX2/unclassified
    differ by ~8x). Returns (its, extrapolated, cross_epoch)."""
    ce = (stats.get("cross_epoch") or {}).get("cpu") or ()
    prior = stats["cpu_prior"].get(cpu)
    if prior is not None:
        return prior, False, cpu in ce
    sibling = market.cpu_sibling(cpu)
    if sibling is not None:
        vals = [(c, p) for c, p in stats["cpu_prior"].items()
                if sibling.search(str(c))]
        if vals:
            return (_median([p for _, p in vals]), True,
                    any(c in ce for c, _ in vals))
    _, isa = market.cpu_profile(cpu)
    vals = [(c, p) for c, p in stats["cpu_prior"].items()
            if market.cpu_profile(c)[1] == isa]
    if not vals:
        return None, False, False
    return (sorted(p for _, p in vals)[len(vals) // 2], True,
            any(c in ce for c, _ in vals))


def est_its(stats, server_id, cpu, gpu, gpu_count=1, use_server=True):
    """Estimated total it/s for a listing: the server's own measured
    history first, else cpu+gpu priors -- a single-side prior is not
    an estimate, it just looks like a tiny rig. Missing sides are
    extrapolated from model-class data. use_server=False gives the
    pure theoretical estimate (display: measured vs expected).
    gpu_count scales the per-GPU prior for uniform multi-card rigs
    (measured ~2x on a dual 5070 Ti); the server_prior path already
    holds the whole-box measured rate and is returned unscaled.
    Returns (its or None, extrapolated, cross_epoch)."""
    entry = stats["server_prior"].get(server_id)
    if use_server and entry is not None:
        return entry["its"], False, bool(entry.get("cross_epoch"))
    cpu_p, cpu_x, cpu_c = _cpu_est(stats, cpu)
    gpu_p, gpu_x, gpu_c = _gpu_est(stats, gpu)
    if cpu_p is None or gpu_p is None:
        return None, False, False
    return cpu_p + gpu_p * gpu_count, cpu_x or gpu_x, cpu_c or gpu_c


def _value(its, cfg):
    """Sublinear payout weight: jetski pays by rig speed but with
    diminishing returns, so value ranks on its**its_exponent."""
    return its ** cfg["its_exponent"] if its is not None else None


def predicted_return(usd_hr, hourly, hours, creation_fee=None):
    """Forecast gross/cost for the actual term, including a fresh fee."""
    if usd_hr is None or hours <= 0:
        return None
    if creation_fee is None:
        creation_fee = _CREATION_FEE_USD
    values = (float(usd_hr), float(hourly), float(hours),
              float(creation_fee))
    if not all(math.isfinite(v) and v >= 0 for v in values):
        return None
    effective_hr = float(hourly) + float(creation_fee) / hours
    return float(usd_hr) / effective_hr if effective_hr > 0 else None


def forecast_profit(usd_hr, hourly, hours=None, creation_fee=None):
    """Net USD/hour; only prospective rentals pay a fresh creation fee."""
    if usd_hr is None or (hours is not None and hours <= 0):
        return None
    if creation_fee is None:
        creation_fee = _CREATION_FEE_USD
    revenue, cost = float(usd_hr), float(hourly)
    if hours is not None:
        cost += float(creation_fee) / hours
    if not all(math.isfinite(v) and v >= 0 for v in (revenue, cost)):
        return None
    return revenue - cost


def pace(state, now, cfg):
    """How much burn/hr the remaining budget sustains to the epoch
    end. Money already committed to active slots counts back into
    the pool: what matters is burn/hr, not the raw remainder, and a
    slot's cost stops at its deadline."""
    hours_to_epoch = max(
        0.0, (state["epoch_ends_at"] - now).total_seconds() / 3600)
    active_left_usd = 0.0
    slot_burn_hr = 0.0
    probe_hold_usd = 0.0
    for r in state["rentals"]:
        if r["role"] == "probe":
            # a probe's full-term quote sits in committed, but it is
            # cancelled inside the free window unless promoted --
            # don't let it squeeze the headroom of the probes after it
            probe_hold_usd += float(r["quoted_cost_usd"])
            continue
        if r["role"] not in ("slot", "kept", "control"):
            continue
        hourly = float(r["hourly_usd"])
        slot_burn_hr += hourly
        deadline = r["deadline_at"]
        if deadline is None and r.get("mode") == "spot":
            # spot has no term: bound its committed burn at the nearer
            # of the phase and epoch ends, not zero
            deadline = min(state["phase_ends_at"],
                           state["epoch_ends_at"])
        if deadline is not None:
            active_left_usd += hourly * max(
                0.0, (deadline - now).total_seconds() / 3600)
    money = float(state["remaining"]) + active_left_usd + probe_hold_usd
    sustainable_hr = (money / hours_to_epoch
                      if hours_to_epoch > 0 else 0.0)
    return {"hours_to_epoch": hours_to_epoch,
            "active_left_usd": active_left_usd,
            "probe_hold_usd": probe_hold_usd,
            "money": money,
            "sustainable_hr": sustainable_hr,
            "slot_burn_hr": slot_burn_hr,
            "headroom_hr": sustainable_hr - slot_burn_hr}


def value_model(db, phase, cfg, rentals_rows, now=None, active=None):
    """Provisional dollar forecast, calibrated to wallet-wide pool value.

    Raw-share and counted-solution updates are not synchronized. Never
    use interval regressions to learn a payout curve automatically.
    Even with a fresh snapshot the per-worker curve is still a prior.
    """
    now = now or utcnow()
    empty = {"source": "unavailable", "P": None, "w_bar": None,
             "S": None, "beta": None, "k": None, "its_ref": None,
             "usd_hr": None}
    if db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table'"
            " AND name='pool_snapshots'").fetchone() is None:
        return empty
    snap = db.execute(
        "SELECT shares, counted, usd_per_counted, epoch, source_ts FROM"
        " pool_snapshots ORDER BY source_ts DESC LIMIT 1").fetchone()
    if snap is None or snap["usd_per_counted"] is None \
            or not snap["shares"] or snap["counted"] is None:
        return {**empty,
                "P": (float(snap["usd_per_counted"])
                      if snap is not None
                      and snap["usd_per_counted"] is not None
                      else None)}
    age = (now - _dt(snap["source_ts"])).total_seconds()
    if not -120 <= age <= POOL_VALUE_MAX_AGE_MIN * 60:
        return empty
    P = float(snap["usd_per_counted"])
    w_bar = snap["counted"] / snap["shares"]
    if not all(math.isfinite(v) and v >= 0
               for v in (P, w_bar)) or snap["epoch"] is None:
        return empty
    active = active or {}
    total_hours = 0.0
    total_shares = 0
    its_hours = []
    for row in rentals_rows:
        if row["role"] not in ("slot", "kept"):
            continue
        created = _dt(row["created_at"])
        ended = _dt(row["ended_at"])
        hours = max(0.0, ((ended or now) - created)
                    .total_seconds() / 3600)
        total_hours += hours
        r = active.get(row["order_id"])
        if r is not None:
            total_shares += sum(c for _, c in r["share_arrivals"])
            if r["its_total"]:
                its_hours.append((r["its_total"], hours))
        else:
            good = [(ts, s) for ts, s in
                    _observations(db, row["order_id"]) if ts <= now]
            total_shares += sum(c for _, c in share_arrivals(good))
            its = [v for _, s in good
                   for v in [_its_total(s)] if v]
            if its:
                its_hours.append((_median(its), hours))
    S = (total_shares / total_hours
         if total_hours >= 6 else cfg["default_shares_per_hour"])
    beta = cfg["its_exponent"]
    k = None
    # production-weighted anchor: the mean its**beta per rig-hour of
    # measured history. The curve then passes through the fleet's
    # actual output center -- S is an hours-weighted mean, so the
    # anchor must be too; a median-rig anchor let a few dead or short
    # rentals drag every forecast just by sitting in the table.
    if its_hours:
        its_hours_total = sum(h for _, h in its_hours)
        its_ref_beta = (sum(m ** beta * h for m, h in its_hours)
                        / its_hours_total) if its_hours_total > 0 \
            else None
    else:
        its_ref_beta = None
    if its_ref_beta is None or not math.isfinite(its_ref_beta) \
            or its_ref_beta <= 0:
        its_ref_beta = 170.0 ** beta
    its_ref = its_ref_beta ** (1.0 / beta) if beta > 0 else None
    source = "prior"

    def usd_hr(its):
        if its is None:
            return None
        if not math.isfinite(its) or its < 0:
            return None
        return S * w_bar * its ** beta / its_ref_beta * P

    return {"source": source, "P": P, "w_bar": w_bar, "S": S,
            "beta": beta, "k": k, "its_ref": its_ref,
            "usd_hr": usd_hr, "confidence": "provisional"}


def rental_cost_usd(row, cfg):
    """What an ended rental actually cost us: hourly x duration plus
    the creation fee once it outlived the free window."""
    seconds = Decimal(str(
        (_dt(row["ended_at"]) - _dt(row["created_at"]))
        .total_seconds()))
    cost = _dec(row["hourly_usd"]) * seconds / Decimal(3600)
    if seconds > Decimal(cfg["free_window_min"] * 60):
        cost += _dec(row["creation_fee_usd"])
    return cost


def committed_usd(db, phase_id, now, cfg):
    """USD spoken for: the cost bound of each active rental, plus for
    ended ones hourly x duration (+ creation fee once it outlived the
    free window)."""
    total = Decimal(0)
    for row in db.execute(
            "SELECT * FROM experiment_rentals WHERE phase_id=?",
            (phase_id,)):
        if row["ended_at"] is None:
            total += _dec(row["quoted_cost_usd"])
        else:
            total += rental_cost_usd(row, cfg)
    return total


def _tree_stats(db, phase_id, limit=80):
    """(dead_trees, lifetime_min) from the phase's archived miner
    logs. Trees are pool-assigned shared work targets that rotate on
    a global clock (~15-25min, synchronized across rigs), and a dead
    tree starves every rig that lands on it whatever its speed -- so
    a tree is dead evidence once >=2 distinct orders hashed on it
    (its>0) and produced nothing. lifetime_min is the median closed
    segment duration: the measured rotation period cooldowns should
    key on. Bounded [8,30], default 20."""
    trees_seen, lives = {}, []
    if not db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table'"
            " AND name='rig_log_chunks'").fetchone():
        return set(), 20.0
    orders = db.execute(
        "SELECT r.order_id FROM experiment_rentals r"
        " WHERE r.phase_id=? AND EXISTS(SELECT 1 FROM rig_log_chunks c"
        "  WHERE c.order_id=r.order_id) ORDER BY r.order_id DESC"
        " LIMIT ?", (phase_id, limit)).fetchall()
    for (oid,) in orders:
        segs = trees.tree_segments(db, oid)
        if not segs:
            continue
        last = db.execute(
            "SELECT status_json FROM rig_observations WHERE order_id=?"
            " AND status_json IS NOT NULL ORDER BY id DESC LIMIT 1",
            (oid,)).fetchone()
        shares = its = 0.0
        if last:
            d = json.loads(last[0])
            shares = (d.get("shares") or {}).get("submitted") or 0
            its = (d.get("its") or {}).get("gpu") or 0
        # an order contributes to a tree's dead-evidence when it
        # hashed on it and produced nothing at all -- positive-share
        # orders can't be attributed per-segment without arrival
        # timestamps, but they disqualify the tree by the same token
        for idx, s in enumerate(segs):
            rec = trees_seen.setdefault(
                s["tree_id"], {"dry": 0, "produced": 0})
            if shares > 0:
                rec["produced"] += 1
            elif its > 0:
                rec["dry"] += 1
            # every segment boundary is the pool's global rotation
            # clock ticking: collect the instant, not the span (a
            # first segment starts mid-rotation and would bias a
            # per-segment duration short)
            if s["end_ts"] is not None:
                try:
                    bound = _dt(s["end_ts"])
                    if bound is not None:
                        lives.append(bound)
                except (TypeError, ValueError):
                    pass
    # boundary instants collapse into rotation events (rigs observe
    # the same tick within a couple minutes of each other), then the
    # median inter-event gap is the rotation period
    instants = sorted(set(lives))
    # scout reports (a home-PC miner tailing its own log) are free
    # evidence: a run of reports on one tree where the scout hashed
    # (its>0) and shares never grew counts as a dry vote exactly like
    # a rental order's; share growth means the tree is alive. Each
    # tree_id transition is also a rotation boundary the fleet never
    # had to rent to observe.
    if db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table'"
            " AND name='tree_reports'").fetchone():
        reps = db.execute(
            "SELECT ts, tree_id, its, shares FROM tree_reports"
            " ORDER BY id DESC LIMIT 400").fetchall()

        def vote(run):
            """A scout run convicts only if it sampled enough
            hashrate-time to plausibly see shares: share rate runs
            ~0.1 sh/its/h, so require >=1.5 expected shares
            (max_its * span_min >= 900). Below that -- e.g. a 1-thread
            home-PC scout -- flat shares carry no information and the
            run only counts if it DID produce (a share is a share)."""
            span = (run["end"] - run["start"]).total_seconds() / 60
            if run["grew"]:
                rec = trees_seen.setdefault(
                    run["tid"], {"dry": 0, "produced": 0})
                rec["produced"] += 1
            elif span >= 10 and run["its_ok"] \
                    and run["max_its"] * span >= 900:
                rec = trees_seen.setdefault(
                    run["tid"], {"dry": 0, "produced": 0})
                rec["dry"] += 1

        run = None
        for r in reversed(reps):
            ts, tid, its, shares = (r["ts"], r["tree_id"],
                                    r["its"] or 0.0, r["shares"] or 0.0)
            if run is None or run["tid"] != tid:
                if run is not None:
                    vote(run)
                run = {"tid": tid, "start": _dt(ts), "end": _dt(ts),
                       "first": shares, "max_its": its,
                       "its_ok": its > 0, "grew": 0}
                continue
            run["end"] = _dt(ts)
            run["max_its"] = max(run["max_its"], its)
            run["its_ok"] = run["its_ok"] or its > 0
            if shares > run["first"]:
                run["grew"] = 1
        if run is not None:
            vote(run)
        for prev, cur in zip(reversed(reps), list(reversed(reps))[1:]):
            if prev["tree_id"] != cur["tree_id"]:
                bound = _dt(cur["ts"])
                if bound is not None:
                    instants.append(bound)
        instants.sort()
    dead = {tid for tid, rec in trees_seen.items()
            if rec["dry"] >= 2 and rec["produced"] == 0}
    events = []
    for t in instants:
        if not events or (t - events[-1]) > _minutes(3):
            events.append(t)
    gaps = [(b - a).total_seconds() / 60
            for a, b in zip(events, events[1:])
            if 5 <= (b - a).total_seconds() / 60 <= 120]
    life = statistics.median(gaps) if gaps else 20.0
    return dead, min(30.0, max(8.0, life))


def upgrade_retryable_servers(db, phase_id, now, cfg):
    """Servers currently cleared for a bounded re-audition (set only)."""
    return upgrade_retryable_state(db, phase_id, now, cfg)[0]


def upgrade_retryable_state(db, phase_id, now, cfg, cooldown_min=None):
    """(retryable set, pending-until map). Servers whose every phase
    burst probe is a confirmed-ended audition that its own burst's
    standing judgement either explicitly tagged as a healthy
    no-share draw or recorded as a losing finder -- both single-draw
    outcomes, neither a hardware verdict.
    Bounded by upgrade_tree_max_attempts sampled orders and
    upgrade_cooldown_min spacing measured from the latest of each
    confirmed end and its judgement; pending maps still-cooling
    qualifiers to their eligible-at time. Read-only; legacy
    burst_judged events without tree_retry_order_ids are UNKNOWN and
    never make a server retryable, malformed optional metadata
    disqualifies rather than crashes, and any phase rental that is
    not a confirmed audition probe keeps the server excluded."""
    def _aware(value):
        """Parse a stored timestamp; naive or malformed values are
        not evidence."""
        try:
            dt = _dt(value)
        except (TypeError, ValueError):
            return None
        if dt is None or dt.tzinfo is None \
                or dt.utcoffset() is None:
            return None
        return dt

    rows = db.execute(
        "SELECT r.order_id, r.server_id, r.burst_id, r.role,"
        " r.created_at, r.ended_at, r.end_reason, b.trigger"
        " FROM experiment_rentals r"
        " LEFT JOIN experiment_bursts b"
        " ON b.burst_id=r.burst_id AND b.phase_id=r.phase_id"
        " WHERE r.phase_id=? ORDER BY r.order_id",
        (phase_id,)).fetchall()
    judgements = {}
    for ts, detail_json in db.execute(
            "SELECT ts, detail_json FROM experiment_events"
            " WHERE phase_id=? AND kind='burst_judged' ORDER BY id",
            (phase_id,)):
        try:
            detail = json.loads(detail_json)
        except (TypeError, ValueError):
            continue
        if not isinstance(detail, dict):
            continue
        bid = detail.get("burst_id")
        if not isinstance(bid, int) or isinstance(bid, bool):
            continue
        # the latest event per burst is the judgement that stands
        judgements[bid] = (ts, detail)
    # burst_id -> (judgement ts, tagged order ids, finder order ids)
    # for each burst's standing judgement. A probe that produced
    # shares and still lost was only sampled on one tree draw -- or,
    # worse, scored on missing telemetry -- so a finder that ended
    # inside its free window earns the same bounded re-audition a
    # tagged no-share draw does, whatever the verdict: under
    # kept_original/renewed the incumbent out-drew it, under
    # no_vacancy no seat ever existed, and under a promoting verdict
    # it lost to a better challenger on the same single draw. The
    # forecast gate at scan time still applies, so a genuinely slow
    # server simply never re-triggers.
    tagged = {}
    for bid, (ts, detail) in judgements.items():
        ids = detail.get("tree_retry_order_ids")
        if not isinstance(ids, list):
            ids = []
        finder_list = detail.get("finders")
        if not isinstance(finder_list, list):
            finder_list = []
        tagged[bid] = (
            ts,
            {oid for oid in ids
             if isinstance(oid, int) and not isinstance(oid, bool)},
            {oid for oid in finder_list
             if isinstance(oid, int) and not isinstance(oid, bool)})
    by_server = {}
    for r in rows:
        by_server.setdefault(r["server_id"], []).append(r)
    free_window = _minutes(cfg["free_window_min"])
    retryable = set()
    pending = {}
    for sid, srows in by_server.items():
        # a same_tree early cancel never sampled a new draw: it spends
        # no attempt but still counts toward spacing so a stuck tree
        # can't trigger an instant create/cancel loop every cycle.
        # only audition rows qualify or spend attempts -- a dry probe
        # or ordinary slot rental says nothing about upgrade
        # worthiness and must not disqualify the server -- but any
        # ended probe still counts toward spacing, since a just-tried
        # rig is probably still on the same tree draw. A pin or
        # manual burst is an audition the operator asked for, so
        # those rows qualify and spend attempts the same way.
        # every burst-placed probe is an audition draw, whatever the
        # burst's trigger: a healthy-but-shareless dry pull says the
        # same thing about tree luck as a targeted upgrade miss.
        # Non-burst rentals (slots, burst-less manual rows) never
        # qualify -- but an ended probe row still counts toward
        # spacing so a just-tried rig can't re-trigger instantly.
        aud = [r for r in srows
               if r["role"] == "probe" and r["trigger"] is not None]
        sampled = [r for r in aud if r["end_reason"] != "same_tree"]
        if not aud or len(sampled) >= cfg["upgrade_tree_max_attempts"]:
            continue
        # the cooldown is one measured tree rotation, not a fixed
        # clock: a retried rig's last draw is stale evidence the
        # moment the pool reassigns
        spacing = _minutes(
            cooldown_min or cfg["upgrade_cooldown_min"])
        ends = []
        ok = True
        for r in srows:
            if not (r["role"] == "probe"
                    and r["trigger"] is not None):
                other_end = _aware(r["ended_at"])
                if r["role"] == "probe" and other_end is not None \
                        and other_end <= now:
                    ends.append(other_end)
                continue
            # every audition row must be a probe that provably ended
            # inside its free window
            if r["role"] != "probe":
                ok = False
                break
            created = _aware(r["created_at"])
            ended = _aware(r["ended_at"])
            if created is None or ended is None \
                    or created > now or ended > now \
                    or not timedelta(0) <= ended - created \
                        < free_window:
                ok = False
                break
            if r["end_reason"] == "same_tree":
                ends.append(ended)
                continue
            if r["end_reason"] not in ("probe_no_share",
                                       "probe_deadline",
                                       "probe_lost"):
                ok = False
                break
            # tagged healthy by that burst's own standing judgement,
            # or a share-finder that lost on a single draw
            judgement = tagged.get(r["burst_id"])
            if judgement is None:
                ok = False
                break
            if r["end_reason"] == "probe_lost":
                if r["order_id"] not in judgement[2]:
                    ok = False
                    break
            elif r["order_id"] not in judgement[1]:
                ok = False
                break
            judge_ts = _aware(judgement[0])
            if judge_ts is None or judge_ts > now \
                    or judge_ts < created:
                ok = False
                break
            ends += [ended, judge_ts]
        if ok and ends:
            eligible_at = max(ends) + spacing
            if eligible_at <= now:
                retryable.add(sid)
            else:
                pending[sid] = eligible_at
    return retryable, pending


def _upgrade_cooldown_judged(db, phase_id, now, cfg):
    """server_id -> latest non-promoting upgrade judgement ts inside
    the cooldown window (the audit the cooldown runs from). Anti-
    thrash for dominant-but-unrentable challengers -- the measured
    tree lifetime doesn't apply to a server never auditioned."""
    judged = {}
    for sid, ts in db.execute(
            "SELECT json_extract(b.detail_json,'$.server_id'),"
            " MAX(j.ts)"
            " FROM experiment_events b"
            " JOIN experiment_events j"
            " ON j.phase_id=b.phase_id AND j.kind='burst_judged'"
            " AND json_extract(j.detail_json,'$.burst_id')="
            "     json_extract(b.detail_json,'$.burst_id')"
            " AND json_extract(j.detail_json,'$.verdict')<>'upgraded'"
            " WHERE b.phase_id=? AND b.kind='burst_started'"
            " AND json_extract(b.detail_json,'$.trigger')='upgrade'"
            " AND j.ts>=?"
            " AND json_extract(b.detail_json,'$.server_id')"
            "     IS NOT NULL"
            " GROUP BY json_extract(b.detail_json,'$.server_id')",
            (phase_id,
             _iso(now - _minutes(cfg["upgrade_cooldown_min"])))):
        dt = _dt(ts)
        if sid is not None and dt is not None:
            judged[sid] = dt
    return judged


def build_state(db, phase, now, cfg, apply_endings=True, say=print):
    """Everything decide() needs, derived from the db. When
    apply_endings, rentals whose fleet row ended are closed out first
    (ended_at + end_reason + an `ended` event)."""
    phase_id = phase["phase_id"]
    listings, snapshot_at = latest_listings(db)
    inventory = orders.read_inventory(db)
    paused_ids = orders.paused_order_ids(inventory, now) or set()
    rentals = []
    for row in db.execute(
            "SELECT * FROM experiment_rentals WHERE phase_id=? "
            "AND ended_at IS NULL ORDER BY order_id", (phase_id,)):
        state = _rental_state(db, phase, row, now, cfg, listings,
                              paused_ids=paused_ids)
        ended_reason = None
        if state["provision_state"] in ENDED_STATES:
            ended_reason = state["provision_state"]
            if ended_reason == "missing" \
                    and _row_mode(row) == "spot":
                # a spot order that was outbid simply disappears from
                # Clore's inventory
                ended_reason = "outbid"
        elif state["deadline_at"] is not None \
                and state["deadline_at"] <= now:
            ended_reason = "term_end"
        if ended_reason is None and state["paused"]:
            if _row_mode(row) == "spot":
                # an outbid spot order stays listed while paused --
                # account it ended without a cancel POST so the
                # standing bid survives for a possible resume
                ended_reason = "outbid"
            elif apply_endings:
                # an on-demand pause may be transient (e.g. account
                # balance); keep the row, just make it visible
                _emit_throttled(
                    db, now, phase_id, row["order_id"],
                    state["server_id"], "paused",
                    {"mode": _row_mode(row),
                     "age_min": int(
                         state["age"].total_seconds() // 60)},
                    "mode", _row_mode(row), say=say)
        if ended_reason is not None:
            if apply_endings:
                with db:
                    db.execute(
                        "UPDATE experiment_rentals SET ended_at=?, "
                        "end_reason=? WHERE order_id=?",
                        (_iso(now), ended_reason, row["order_id"]))
                emit(db, now, phase_id, row["order_id"],
                     state["server_id"], "ended",
                     {"reason": ended_reason,
                      "its": state["its_total"],
                      "shares": sum(
                          c for _, c in state["share_arrivals"]),
                      "age_min": int(
                          state["age"].total_seconds() // 60)},
                     say=say)
            continue
        rentals.append(state)
    # a re-audition probe can be compared against the tree its
    # server's previous attempt drew: an identical draw carries no
    # new information, so decide() can cancel it immediately.
    prev_order = {}
    last_on = {}
    for row in db.execute(
            "SELECT server_id, order_id FROM experiment_rentals"
            " WHERE phase_id=? ORDER BY order_id", (phase_id,)):
        prev_order[row["order_id"]] = last_on.get(row["server_id"])
        last_on[row["server_id"]] = row["order_id"]
    for r in rentals:
        poid = prev_order.get(r["order_id"])
        # every rental learns its current tree when the log carries
        # one: dead-tree evidence applies to probes and slots alike,
        # while the previous-draw comparison stays probe-only
        cur = trees.current_tree(db, r["order_id"])
        r["tree_id"] = (cur or {}).get("tree_id")
        # when the rig entered its current tree -- bounds how long a
        # dead-tree sitter may wait for the rotation's free redraw
        r["tree_since"] = _dt((cur or {}).get("ts"))
        if r["role"] != "probe" or poid is None:
            continue
        prev = trees.current_tree(db, poid)
        r["prev_tree_id"] = (prev or {}).get("tree_id")
    if apply_endings:
        # a rental's first accepted share is the moment it proves
        # itself; log it once per order
        shared = {r["order_id"] for r in db.execute(
            "SELECT order_id FROM experiment_events WHERE phase_id=?"
            " AND kind='first_share'", (phase_id,))}
        for r in rentals:
            if r["last_share_at"] is not None \
                    and r["order_id"] not in shared:
                emit(db, now, phase_id, r["order_id"], r["server_id"],
                     "first_share",
                     {"role": r["role"],
                      "age_min": round(r["age"].total_seconds() / 60, 1),
                      "its": r["its_total"]}, say=say)
    committed = committed_usd(db, phase_id, now, cfg)
    budget = _dec(phase["budget_usd"])
    stats = history_stats(db, server_listings(db), now=now)
    vm = value_model(
        db, phase, cfg,
        db.execute(
            "SELECT * FROM experiment_rentals WHERE phase_id=?",
            (phase_id,)).fetchall(),
        now=now,
        active={r["order_id"]: r for r in rentals})
    slot_target = _slot_target(
        rentals, now, cfg, vm, stats.get("score_ref"))
    # an outbid spot order that Clore resumed is back in inventory
    # unpaused while its row sits ended as 'outbid': re-open it as a
    # re-baselined spot probe for a normal audition -- or, when the
    # phase can't safely carry it, cancel it so nothing bills
    # unaccounted. build_state assumes a single unjudged burst, so a
    # resume while one is pending can never queue
    reactivate_cancels = []
    if apply_endings:
        fresh_ids = orders.fresh_order_ids(inventory, now)
        inv_by_id = {o["order_id"]: o
                     for o in inventory.get("orders") or []
                     if isinstance(o, dict)
                     and isinstance(o.get("order_id"), int)}
        if fresh_ids is not None:
            for row in db.execute(
                    "SELECT * FROM experiment_rentals WHERE phase_id=?"
                    " AND end_reason='outbid' ORDER BY order_id",
                    (phase_id,)):
                item = inv_by_id.get(row["order_id"])
                if row["order_id"] not in fresh_ids \
                        or orders.order_paused(item or {}):
                    continue
                unjudged_open = db.execute(
                    "SELECT 1 FROM experiment_bursts WHERE phase_id=?"
                    " AND judged_at IS NULL LIMIT 1",
                    (phase_id,)).fetchone()
                ok = (_row_mode(row) == "spot"
                      and now < _dt(phase["ends_at"])
                      and snapshot_at is not None
                      and now - snapshot_at
                          <= _minutes(cfg["stale_market_min"])
                      and budget - committed > 0
                      and unjudged_open is None)
                if not ok:
                    reactivate_cancels.append(
                        {"order_id": row["order_id"],
                         "server_id": row["server_id"],
                         "tier": row["tier"]})
                    continue
                slots = [r for r in rentals if r["role"] == "slot"]
                trigger_oid = None
                if len(slots) >= slot_target:
                    trigger_oid = min(
                        slots,
                        key=lambda r: (r["last_share_at"] is None,
                                       r["its_total"] or 0)
                    )["order_id"]
                paused_min = round(
                    (now - _dt(row["ended_at"])).total_seconds()
                    / 60, 1)
                with db:
                    cur = db.execute(
                        "INSERT INTO experiment_bursts"
                        "(phase_id, started_at, trigger,"
                        " trigger_order_id, n_target)"
                        " VALUES (?,?,?,?,?)",
                        (phase_id, _iso(now), "reactivated",
                         trigger_oid, 1))
                    burst_id = cur.lastrowid
                    db.execute(
                        "UPDATE experiment_rentals SET ended_at=NULL,"
                        " end_reason=NULL, role='probe', burst_id=?,"
                        " resumed_at=? WHERE order_id=?",
                        (burst_id, _iso(now), row["order_id"]))
                emit(db, now, phase_id, trigger_oid, None,
                     "burst_started",
                     {"burst_id": burst_id, "trigger": "reactivated",
                      "n": 1, "y_min": row["y_min"]}, say=say)
                emit(db, now, phase_id, row["order_id"],
                     row["server_id"], "reactivated",
                     {"burst_id": burst_id, "paused_min": paused_min},
                     say=say)
                fresh_row = db.execute(
                    "SELECT * FROM experiment_rentals"
                    " WHERE order_id=?", (row["order_id"],)).fetchone()
                rentals.append(
                    _rental_state(db, phase, fresh_row, now, cfg,
                                  listings, paused_ids=paused_ids))
    bursts = [dict(b) for b in db.execute(
        "SELECT * FROM experiment_bursts WHERE phase_id=? "
        "ORDER BY burst_id", (phase_id,))]
    burst_meta = {}
    for row in db.execute(
            "SELECT detail_json FROM experiment_events WHERE phase_id=?"
            " AND kind='burst_started'", (phase_id,)):
        detail = json.loads(row["detail_json"])
        if detail.get("burst_id") is not None:
            burst_meta[detail["burst_id"]] = detail
    for b in bursts:
        b["started_at"] = _dt(b["started_at"])
        b["judged_at"] = _dt(b["judged_at"])
        meta = burst_meta.get(b["burst_id"]) or {}
        b["y_min"] = meta.get("y_min")
        b["target_server"] = meta.get("server_id")
    unjudged = [b for b in bursts if b["judged_at"] is None]
    last_burst_at = (max(b["started_at"] for b in bursts)
                     if bursts else None)
    last_started_event = db.execute(
        "SELECT MAX(id) FROM experiment_events WHERE phase_id=? "
        "AND kind='burst_started'", (phase_id,)).fetchone()[0]
    burst_request = db.execute(
        "SELECT detail_json FROM experiment_events WHERE phase_id=? "
        "AND kind='burst_requested' AND id>? ORDER BY id DESC LIMIT 1",
        (phase_id, last_started_event or 0)).fetchone()
    burst_probes = {b["burst_id"]: [] for b in unjudged}
    if unjudged:
        by_order = {r["order_id"]: r for r in rentals}
        marks = ", ".join("?" for _ in unjudged)
        for prow in db.execute(
                "SELECT order_id, server_id, role, hourly_usd,"
                " created_at, ended_at, burst_id FROM experiment_rentals"
                f" WHERE phase_id=? AND burst_id IN ({marks})",
                (phase_id,
                 *[b["burst_id"] for b in unjudged])):
            active = by_order.get(prow["order_id"])
            if active is not None:
                probe = dict(active)
                probe["ended"] = False
            else:
                probe = {"order_id": prow["order_id"],
                         "server_id": prow["server_id"],
                         "role": prow["role"],
                         "hourly_usd": _dec(prow["hourly_usd"]),
                         "age": now - _dt(prow["created_at"]),
                         "t0": None, "share_arrivals": [],
                         "ended": True}
            burst_probes[prow["burst_id"]].append(probe)
    last_obs = [r["last_obs_at"] for r in rentals
                if r["last_obs_at"] is not None]
    telemetry_age_s = ((now - max(last_obs)).total_seconds()
                       if last_obs else None)
    scored = {}
    for sid, listing in listings.items():
        scored[sid] = market.score_listing(listing)
    dead_ts, life_min = _tree_stats(db, phase_id)
    # the scout agent's latest report: which tree the pool is handing
    # out right now, observed for free on a home-PC miner. Fresh means
    # <15min old -- beyond a rotation the report no longer describes
    # the current offering.
    agent = None
    if db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table'"
            " AND name='tree_reports'").fetchone():
        rep = db.execute(
            "SELECT ts, source, epoch, tree_id, seed, its, shares"
            " FROM tree_reports ORDER BY id DESC LIMIT 1").fetchone()
        if rep is not None:
            rep_dt = _dt(rep["ts"])
            if rep_dt is not None and (now - rep_dt) < _minutes(15):
                agent = dict(rep)
    retryable_set, retryable_pending = upgrade_retryable_state(
        db, phase_id, now, cfg, cooldown_min=life_min)
    # order_id -> latest drought_hold ts: an incumbent that produced
    # shares this order and is still hashing gets one reprieve when a
    # whole audition turns up silent -- the silence then reads as a
    # crediting/tree drought, not dead hardware. Consumed once.
    drought_holds = {}
    for oid, ts in db.execute(
            "SELECT b.trigger_order_id, MAX(j.ts)"
            " FROM experiment_bursts b"
            " JOIN experiment_events j"
            " ON j.phase_id=b.phase_id AND j.kind='burst_judged'"
            " AND json_extract(j.detail_json,'$.burst_id')=b.burst_id"
            " AND json_extract(j.detail_json,'$.verdict')"
            "     ='drought_hold'"
            " WHERE b.phase_id=? AND b.trigger_order_id IS NOT NULL"
            " GROUP BY b.trigger_order_id", (phase_id,)):
        dt = _dt(ts)
        if oid is not None and dt is not None:
            drought_holds[oid] = dt
    tried_reset_at = db.execute(
        "SELECT MAX(ts) FROM experiment_events WHERE phase_id=?"
        " AND kind='tried_reset'", (phase_id,)).fetchone()[0]
    # lull streak: consecutive slot endings on yield rules. Endings
    # unrelated to yield (host cancel, fault, probe judgement) are
    # skipped; a slot that survived past the free window breaks it.
    # A live slot still producing shares means the pool is alive and
    # the failures were unlucky draws -- no pause.
    yield_dead = {"low_yield", "low_yield_after_x", "dry_after_x",
                  "dry"}
    lull_streak = 0
    lull_last_end = None
    for row in db.execute(
            "SELECT role, end_reason, created_at, ended_at FROM"
            " experiment_rentals WHERE phase_id=?"
            " AND ended_at IS NOT NULL"
            " ORDER BY ended_at DESC LIMIT 30", (phase_id,)):
        if row["end_reason"] == "outbid":
            # losing a spot bid is market competition, not a yield
            # failure -- it neither extends nor breaks the streak
            continue
        if row["role"] == "slot" and row["end_reason"] in yield_dead:
            lull_streak += 1
            if lull_last_end is None:
                lull_last_end = _dt(row["ended_at"])
        elif row["role"] == "slot" and row["created_at"] \
                and row["ended_at"] and \
                _dt(row["ended_at"]) - _dt(row["created_at"]) > \
                _minutes(cfg["free_window_min"]):
            break
    producing = _producing_slots(rentals, now, cfg) > 0
    lull_until = None
    if lull_streak >= cfg["lull_streak"] and not producing \
            and lull_last_end is not None:
        until = lull_last_end + _minutes(cfg["lull_pause_min"])
        if until > now:
            lull_until = until
    tried = {row["server_id"] for row in db.execute(
        "SELECT DISTINCT server_id FROM experiment_rentals"
        " WHERE phase_id=? AND created_at>=?",
        (phase_id, tried_reset_at or "1970-01-01"))}
    gpu_trials = {}
    for row in db.execute(
            "SELECT server_id FROM experiment_rentals"
            " WHERE phase_id=? AND created_at>=?",
            (phase_id, tried_reset_at or "1970-01-01")):
        g = (scored.get(row["server_id"]) or {}).get("gpu") or ""
        gpu_trials[g] = gpu_trials.get(g, 0) + 1
    state = {
        "phase_id": phase_id,
        "phase_ends_at": _dt(phase["ends_at"]),
        "budget": budget,
        "reserve": _dec(phase["reserve_usd"]),
        "committed": committed,
        "remaining": budget - committed,
        "epoch_ends_at": epoch_end(now),
        "active_burn_hr": sum(
            r["hourly_usd"] for r in rentals),
        "rentals": rentals,
        "pending_rerents": pending_rerents(db, phase_id),
        "pending_bursts": pending_bursts(db, phase_id),
        "probe_pins": probe_pins(db, phase_id),
        "excluded": excluded_servers(db, phase_id) | set(
            blacklist.active(db, now)),
        "bad_owners": bad_owners(db, scored),
        "quote_rejected": {row["server_id"] for row in db.execute(
            "SELECT DISTINCT server_id FROM experiment_events WHERE"
            " phase_id=? AND kind='rent_failed' AND ts>=?"
            " AND (json_extract(detail_json, '$.stage')='quote'"
            " OR json_extract(detail_json, '$.info.stage') IN"
            " ('lock','secrets','refetch','guards','spot_recheck',"
            " 'attempt','post','reconcile'))",
            (phase_id,
             _iso(now - _minutes(cfg["quote_reject_min"]))))},
        "listings": listings,
        "scored": scored,
        "snapshot_at": snapshot_at,
        "bursts": bursts,
        "unjudged_bursts": unjudged,
        "last_burst_started_at": last_burst_at,
        "burst_request_n": (json.loads(burst_request["detail_json"])
                            .get("n") if burst_request else None),
        "burst_probes": burst_probes,
        "reactivate_cancels": reactivate_cancels,
        "telemetry_age_s": telemetry_age_s,
        "snapshot_age_s": ((now - snapshot_at).total_seconds()
                           if snapshot_at else None),
        "pending_attempts": bool(fleet_store.pending_attempts(db)),
        "stats": stats,
        "pool_share": pool_share_estimate(
            db, rentals, cfg, now, stats=stats, scored=scored,
            remaining=budget - committed),
        "value_model": vm,
        # tried only counts rentals placed after the latest
        # tried_reset event -- a manual reset re-opens the upgrade
        # scan to pre-reset servers while preserving the phase lock
        # for anything tried since
        "tried_servers": tried,
        # measured-hardware coverage this phase: rental samples per
        # GPU model. The probe picker prefers classes under
        # gpu_trial_min samples so priors get a cross-section of
        # actuals; sampled classes compete on score alone
        "gpu_trials": dict(gpu_trials),
        # slots that already got their term-end audition this phase
        "succession_bursts": {row[0] for row in db.execute(
            "SELECT trigger_order_id FROM experiment_bursts"
            " WHERE phase_id=? AND trigger='succession'",
            (phase_id,))},
        # target challengers of upgrade bursts that judged without
        # promoting: in cooldown so a dominant-but-unrentable listing
        # can't re-trigger an audition every burst window. Cooling
        # runs from the completed judgement, not the burst start.
        "upgrade_cooldown": set(_upgrade_cooldown_judged(
            db, phase_id, now, cfg)),
        "upgrade_cooldown_until": {
            sid: judged_at + _minutes(cfg["upgrade_cooldown_min"])
            for sid, judged_at in _upgrade_cooldown_judged(
                db, phase_id, now, cfg).items()},
        "dead_trees": dead_ts,
        "tree_lifetime_min": life_min,
        # the scout agent's fresh report: the tree the pool would
        # hand a new rental right now (None when stale/absent)
        "agent": agent,
        # servers cleared for a bounded re-audition: every phase
        # rental was a healthy-but-no-share upgrade probe the judge
        # explicitly tagged. tried_servers keeps all its semantics;
        # this set only re-opens the upgrade scan.
        # (retryable set, pending-until map): servers cleared for a
        # bounded re-audition now, and those just spacing out
        "upgrade_retryable": retryable_set,
        "upgrade_pending_until": retryable_pending,
        "drought_holds": drought_holds,
        "lull_until": lull_until,
        "lull_streak": lull_streak,
        "slot_target": slot_target,
        # per-server executable spot bids from cached observations
        # (only usable quotes); empty unless spot_enabled
        "spot_quotes": (
            {sid: bid for sid, rec in spot.latest_quotes(db).items()
             if (bid := spot.bid_for(
                 rec, now, premium=cfg["spot_bid_premium"])) is not None}
            if cfg["spot_enabled"] else {}),
        # servers spot-cooled by a recent outbid ending; on-demand
        # eligibility is unaffected
        "spot_outbid_cooldown": (
            {row["server_id"] for row in db.execute(
                "SELECT DISTINCT server_id FROM experiment_rentals"
                " WHERE phase_id=? AND end_reason='outbid'"
                " AND ended_at>=?",
                (phase_id,
                 _iso(now - _minutes(cfg["spot_outbid_min"]))))}
            if cfg["spot_enabled"] else set()),
    }
    # a pin exists to force one audition; a server already tried (and
    # not cleared for re-audition) or phase-excluded (faulted or
    # failed at a server-fault stage) has nothing left to force --
    # drop the pin rather than let it sit skipped forever. Blacklist
    # and cooldown exclusions are time-bounded, so those pins stay.
    phase_excluded = excluded_servers(db, phase_id)
    for sid in list(state["probe_pins"]):
        # tried/cooling servers are NOT dropped: a pin is the
        # operator's manual reset, honored with one bounded
        # re-audition per pin (consumed on rent); upgrade_cooldown
        # still spaces it at fire time. Only hard ineligibility
        # (phase exclusion, blacklisted owner) kills a pin.
        if sid in phase_excluded:
            reason = "excluded"
        elif (state["scored"].get(sid) or {}).get("owner") \
                in state["bad_owners"]:
            reason = "bad_owner"
        else:
            continue
        with db:
            db.execute(
                "DELETE FROM probe_pins"
                " WHERE phase_id=? AND server_id=?",
                (phase_id, sid))
        state["probe_pins"].remove(sid)
        emit(db, now, phase_id, None, sid, "unpinned",
             {"server_id": sid, "reason": reason}, say=say)
    return state


def _producing_slots(rentals, now, cfg):
    """Slots with share evidence fresh enough to count as producing:
    a share inside dry_after_min means the pool pays for its tree."""
    return sum(1 for r in rentals
               if r["role"] == "slot" and r["last_share_at"] is not None
               and now - r["last_share_at"] < _minutes(
                   cfg["dry_after_min"]))


def realized_usd_h(r, vm, now, score_ref=None):
    """Measured $/h: post-warmup arrivals priced at the pool's
    current counted-share value. None until the model exists.
    score_ref reweights the share count by the rig's measured
    score-per-share against the fleet mean: w_bar prices an
    AVERAGE share, and a rig whose ants score below the fleet's
    earns proportionally less per share (the cheap-rig discount
    the wallet-level counted/shares ratio showed)."""
    price, w_bar = (vm or {}).get("P"), (vm or {}).get("w_bar")
    if not price or not w_bar or r.get("t0") is None:
        return None
    start = r["t0"] + _minutes(2)
    hours = (now - start).total_seconds() / 3600
    if hours <= 0:
        return None
    rate = sum(c for ts, c in r["share_arrivals"] if ts >= start) \
        / hours
    sps, ref = r.get("score_per_share"), score_ref
    if sps and ref:
        rate *= sps / ref
    return rate * w_bar * price


def slot_score(r, cfg, vm, now, score_ref=None):
    """Incumbent score for fleet sizing and the weakest-slot pick:
    realized yield where the window makes it real, forecast
    otherwise."""
    if cfg["score"] == "its":
        return r["its_total"] or 0
    usd_hr_fn = (vm or {}).get("usd_hr")
    if usd_hr_fn is not None:
        revenue = (realized_usd_h(r, vm, now, score_ref)
                   if r["age"] >= _minutes(cfg["actual_yield_min"])
                   else None)
        if revenue is None:
            revenue = usd_hr_fn(r["its_total"])
        if cfg["score"] == "gross":
            return (float(revenue) if revenue is not None
                    else -math.inf)
        profit = forecast_profit(revenue, r["hourly_usd"])
        return profit if profit is not None else -math.inf
    value = _value(r["its_total"], cfg)
    if cfg["score"] == "gross":
        return value or 0.0
    return (value or 0.0) / float(r["hourly_usd"])


def _exceptional_slots(rentals, now, cfg, vm, score_ref=None):
    """Slots standing out from their peers: scored above the mean of
    the OTHER rankable slots by growth_margin. Leave-one-out so a
    uniformly strong fleet still reads as exceptional times -- a
    plain fleet mean would let a weak laggard drag the bar under
    every good rig. A lone rig can't stand out from itself, and
    unrankable rigs (fresh or unmeasured) don't vote."""
    slots = [r for r in rentals
             if r["role"] == "slot" and r["its_total"]
             and r["age"] >= _minutes(cfg["startup_min"])]
    if len(slots) < 2:
        return 0
    scores = [slot_score(r, cfg, vm, now, score_ref) for r in slots]
    total = sum(scores)
    n = len(scores)
    return sum(1 for s in scores
               if s > max(0.0, (total - s) / (n - 1))
               * (1 + cfg["growth_margin"]))


def _slot_target(rentals, now, cfg, vm=None, score_ref=None):
    """Current slot mandate: static always fills to max_rentals;
    dynamic grows the fleet only on exceptional evidence -- each rig
    scoring above the fleet mean by growth_margin earns one hunting
    seat beyond itself, so a normal fleet settles to slot_min and an
    exceptional one keeps auditioning up to max_rentals."""
    if cfg["slot_mode"] == "static":
        return cfg["max_rentals"]
    return min(cfg["max_rentals"],
               max(cfg["slot_min"],
                   _exceptional_slots(rentals, now, cfg, vm,
                                        score_ref) + 1))


def spec_mismatch(listing, status):
    """Only flag explicit, comparable CPU model/allocation differences."""
    specs = (listing or {}).get("specs") or {}
    hardware = status.get("hardware") or {}
    tokens = lambda s: set(re.findall(r"\b\d{4,5}[a-z]{0,3}\b",
                                     str(s or "").lower()))
    offered = tokens(specs.get("cpu"))
    seen = tokens(hardware.get("cpu_model"))
    if offered and seen and offered.isdisjoint(seen):
        return ("spec_mismatch:cpu", {
            "listed": specs["cpu"], "observed": hardware["cpu_model"]})
    threads = rental_policy.offered_threads(specs)
    nproc = hardware.get("nproc")
    if threads and isinstance(nproc, int) and nproc < threads:
        return ("spec_mismatch:threads", {
            "listed": threads, "observed": nproc})
    return None


def _live_order_ids(db, now):
    """Managed-order ids from a fresh good inventory; None when no
    usable snapshot exists -- the difference between a rig that went
    bad and an order Clore ended (e.g. account balance spent)."""
    return orders.fresh_order_ids(orders.read_inventory(db), now=now)


def blacklist_checks(db, state, now, cfg):
    """Evidence for new blocks; reads only and never uses share yield."""
    results = []
    blocked = blacklist.active(db, now)
    listings = {**server_listings(db), **state["listings"]}
    cutoff = now - _minutes(30)
    # A stopped collector supplies no new evidence about a server.
    if state["telemetry_age_s"] is None or \
            state["telemetry_age_s"] > cfg["stale_obs_sec"]:
        return results
    live_ids = _live_order_ids(db, now)
    for r in state["rentals"]:
        sid, oid = r["server_id"], r["order_id"]
        if sid in blocked:
            continue
        # Removing an entry deliberately allows retry; don't recreate it
        # from the same rental's old evidence.
        if db.execute(
                "SELECT 1 FROM experiment_events WHERE order_id=? "
                "AND kind='blacklisted' LIMIT 1", (oid,)).fetchone():
            continue
        recent = r["last_obs_at"]
        if recent is None or (now - recent).total_seconds() > cfg["stale_obs_sec"]:
            continue
        evidence = None
        # Unreachability is evidence against a server only while Clore
        # still reports its order live: an account that runs dry kills
        # every rig at once, and those servers must not be blocked.
        if r["age"] >= _minutes(30) and live_ids is not None \
                and oid in live_ids:
            rows = db.execute(
                "SELECT status_json FROM rig_observations WHERE order_id=? "
                "AND captured_at>=? AND captured_at<=?",
                (oid, _iso(cutoff), _iso(now))).fetchall()
            failed = sum(row["status_json"] is None for row in rows)
            if len(rows) >= 20 and failed / len(rows) > 0.40:
                evidence = ("flaky_ssh", {"failed": failed, "total": len(rows),
                                         "window_minutes": 30})
        fresh = r["obs_age"] is not None and r["obs_age"].total_seconds() \
            <= cfg["stale_obs_sec"]
        listing = listings.get(sid)
        if evidence is None and fresh:
            evidence = spec_mismatch(listing, r["good_obs"][-1][1])
        if evidence:
            results.append((oid, sid, *evidence))
    return results


def _exec_blacklist(db, phase_id, action, now, say):
    bid = blacklist.add(db, action["server_id"], action["reason"], "auto",
                        detail=action.get("detail"), now=now)
    if bid is not None:
        emit(db, now, phase_id, action.get("order_id"), action["server_id"],
             "blacklisted", {"reason": action["reason"],
                             "detail": action.get("detail") or {},
                             "ttl_days": blacklist.AUTO_TTL_DAYS}, say=say)


def _retry_burst(r, now, life_min=None):
    """Re-audition after the cancelled rental's arm: the burst buys
    fresh draws but reserves no vacancy. A box that already produced
    this order waits one measured tree rotation (life_min, ~5m
    floor) -- the hardware is proven, the wait exists only for the
    pool's reassignment clock; an unproven box keeps the full y arm."""
    proven = bool(r.get("share_arrivals"))
    if proven:
        wait = max(5.0, life_min or r["y_min"])
    else:
        wait = r["y_min"]
    return {"for_order": r["order_id"], "server_id": r["server_id"],
            "tier": r["tier"], "y_min": r["y_min"], "proven": proven,
            "due_at": _iso(now + _minutes(wait))}


def _fault_reason(rental):
    """Fault checks for the free window; returns a short tag or None."""
    if rental.get("spec_mismatch"):
        return "spec"
    if rental["last_two_zero_its"]:
        return "zero_its"
    if rental["listing_gpu_count"]:
        if not rental["gpu_its"]:
            return "gpu_its"
        if rental["gpu_count_seen"] is not None \
                and rental["gpu_count_seen"] != rental["listing_gpu_count"]:
            return "gpu_count"
    return None


def _probe_judge_min(mode, cfg):
    """A spot probe cancels free at any time, so its judge window is
    a choice (longer = less Poisson noise), not a free-window race."""
    return (cfg["spot_probe_judge_min"] if mode == "spot"
            else cfg["probe_judge_min"])


def _probe_cancel_min(mode, cfg):
    # keep the same margin the on-demand pair leaves between judging
    # and the hard deadline
    return (_probe_judge_min(mode, cfg)
            + cfg["probe_cancel_by_min"] - cfg["probe_judge_min"])


def decide(now, state, cfg, say=None):
    """Pure: state -> ordered action dicts. One action per rental per
    cycle; cancel actions survive global blocks, rent actions are
    replaced by `blocked` entries. `say`, when given, gets
    decision notes for the reasoning behind each choice (candidate
    pools, skips); run_cycle persists them as `note` events."""
    actions = []
    # a resumed spot order the phase can't carry: it bills while it
    # stands, so the cancel must survive every global block
    for c in state.get("reactivate_cancels") or []:
        actions.append({"type": "cancel", "order_id": c["order_id"],
                        "server_id": c["server_id"],
                        "tier": c["tier"], "reason": "reactivate_blocked",
                        "rerent_at": None})
    telemetry_stale = bool(state["rentals"]) and (
        state["telemetry_age_s"] is None
        or state["telemetry_age_s"] > cfg["stale_obs_sec"])
    blocks = []
    if cfg["paused"]:
        blocks.append("paused")
    if telemetry_stale:
        blocks.append("telemetry_stale")
    if now >= state["phase_ends_at"]:
        blocks.append("phase")
    if state["snapshot_age_s"] is None \
            or state["snapshot_age_s"] > cfg["stale_market_min"] * 60:
        blocks.append("stale_market")
    if state["pending_attempts"]:
        blocks.append("pending")
    if state["remaining"] <= 0:
        blocks.append("budget")
    # a probe near its free-window deadline freezes rentals: a blocking
    # provision wait could push its cancel past the window. In blocks
    # early so a burst (which only emits probe rents) never starts a
    # countdown it can't fill.
    if any(r["role"] == "probe" and r.get("mode") != "spot"
           and r["age"] >= _minutes(
               cfg["probe_cancel_by_min"] - PROBE_RENT_GUARD_MIN)
           for r in state["rentals"]):
        blocks.append("probe_guard")

    # quote term for new rentals: the configured hours, capped at
    # the time left in the epoch. The budget is a ceiling, not a
    # target -- pace() says what burn/hr it sustains, and fills /
    # probes stay inside that headroom rather than spending it down.
    pacing = pace(state, now, cfg)
    hours_to_epoch = pacing["hours_to_epoch"]
    # a new rental's term is bounded by the nearer of the phase end
    # and the epoch end -- under a full hour to either the quote buys
    # the minimum term. There is no last-hour block: shares per
    # budget is the goal, and a fill or probe still produces until
    # the phase ends; it only burns budget sooner.
    hours_to_phase = max(
        0.0, (state["phase_ends_at"] - now).total_seconds() / 3600)
    rent_window_h = min(hours_to_epoch, hours_to_phase)
    term = max(1, min(cfg["hours"], int(rent_window_h)))
    headroom_hr = pacing["headroom_hr"]
    # efficiency floor, relative to the best rentable listing now
    best_prior = next(
        (c["prior"] for c in _candidates(
            state, cfg, {r["server_id"] for r in state["rentals"]}, [],
            term=term, spot_mode=cfg["spot_slot_mode"])
         if c["prior"] is not None), None)
    value_floor = (best_prior * cfg["min_value_ratio"]
                   if best_prior is not None else None)

    # loss_mining drops the viability gates outright -- probes,
    # fills and promotion rank purely on score. _gate_violation
    # still flags what would have failed so the pick is marked.
    def _below_floor(c):
        return not cfg["loss_mining"] and (
            value_floor is not None and (
                c["prior"] is None or c["prior"] < value_floor))

    # Fresh wallet calibration adds a provisional dollar forecast.
    # Both the absolute return gate and relative floor still apply.
    usd_hr_fn = (state.get("value_model") or {}).get("usd_hr")

    def _below_return(c):
        return not cfg["loss_mining"] and usd_hr_fn is not None and (
            c["return_x"] is None or c["return_x"] < cfg["min_return"])

    def _evidenced(c):
        """Estimate rests on measured priors (this server's history or
        its hardware class), not a class-level extrapolation. The
        below-slot_min pace exemption applies only to these -- a
        guessed-at rig still waits for headroom or earns evidence
        through a bounded probe."""
        return c.get("est_its") is not None and not c.get("extrapolated")

    def _gate_violation(c):
        return (value_floor is not None and (
                c["prior"] is None or c["prior"] < value_floor)) or (
                usd_hr_fn is not None and (
                c["return_x"] is None
                or c["return_x"] < cfg["min_return"]))

    def _its_value(its):
        """Value of a measured its_total: predicted $/h when the
        model is available, its**its_exponent otherwise."""
        return (usd_hr_fn(its) if usd_hr_fn is not None
                else _value(its, cfg))

    def _candidate_score(c, hours=term):
        if cfg["score"] == "its":
            return c["est_its"] or 0.0
        if usd_hr_fn is None:
            # gross without the model: the its-proxy value; net: prior
            return (c["value"] or 0.0) if cfg["score"] == "gross" \
                else c["prior"] or 0.0
        if cfg["score"] == "gross":
            return (float(c["usd_hr"]) if c["usd_hr"] is not None
                    else -math.inf)
        profit = forecast_profit(
            c["usd_hr"], c["hourly"], hours,
            c.get("creation_fee", _CREATION_FEE_USD))
        return profit if profit is not None else -math.inf

    def _realized_usd_h(r):
        return realized_usd_h(
            r, state.get("value_model"), now,
            (state.get("stats") or {}).get("score_ref"))

    def _score_unit():
        """Log label for the score values say-lines print."""
        if cfg["score"] == "its":
            return "it/s"
        if usd_hr_fn is None:
            return "v/$"
        return {"net": "net$/h", "gross": "gross$/h"}[cfg["score"]]

    def _slot_score(r):
        return slot_score(
            r, cfg, state.get("value_model"), now,
            (state.get("stats") or {}).get("score_ref"))

    def _upgrade_allowed(sid, pinned=False):
        # a pin is the operator's manual reset: any tried or cooling
        # server gets one bounded re-audition per pin (consumed on
        # rent), the same-tree risk taken as an explicit decision;
        # judged-challenger cooldown still spaces the re-audition
        return (pinned
                or sid not in state["tried_servers"]
                or sid in state.get("upgrade_retryable", set())) \
            and sid not in state["upgrade_cooldown"]

    # concurrent auditions: one unjudged burst per seat at most -- a
    # second expiring slot or a due retry must not queue behind the
    # first audition's full probe window. A seat already under
    # audition never triggers a second burst: the audition in flight
    # IS that seat's retry/upgrade/succession path.
    burst_room = 0
    if now < state["phase_ends_at"] and not blocks \
            and cfg["probes_enabled"]:
        burst_room = max(0, cfg["max_rentals"]
                         - len(state["unjudged_bursts"]))
    busy_seats = {b["trigger_order_id"]
                  for b in state["unjudged_bursts"]}

    def _ctx(r):
        """Outcome context carried into cancel events."""
        return {"its": r["its_total"],
                "shares": sum(c for _, c in r["share_arrivals"]),
                "score_med": r.get("score_med"),
                "score_per_share": r.get("score_per_share"),
                "age_min": int(r["age"].total_seconds() // 60)}

    for r in [] if telemetry_stale else state["rentals"]:
        t0 = r["t0"]
        obs_age = r["obs_age"]
        role = r["role"]
        if role == "probe" \
                and r["age"] >= _minutes(
                    _probe_cancel_min(r.get("mode"), cfg)):
            actions.append({"type": "cancel", "order_id": r["order_id"],
                            "server_id": r["server_id"],
                            "tier": r["tier"], **_ctx(r),
                            "reason": "probe_deadline", "rerent_at": None})
            continue
        if r["recent_errors_only"] and not r.get("paused"):
            actions.append({"type": "cancel", "order_id": r["order_id"],
                            "server_id": r["server_id"],
                            "tier": r["tier"], **_ctx(r),
                            "reason": "unreachable",
                            "unreach": r["unreach"], "rerent_at": None})
            continue
        if role == "probe" and r.get("prev_tree_id") is not None \
                and r.get("tree_id") is not None \
                and r["tree_id"] == r["prev_tree_id"]:
            # the re-audition drew the identical tree as the previous
            # attempt on this server: no new sample to gather, stop
            # the burn inside the free window instead of riding it out.
            actions.append({"type": "cancel", "order_id": r["order_id"],
                            "server_id": r["server_id"],
                            "tier": r["tier"], **_ctx(r),
                            "reason": "same_tree", "rerent_at": None})
            continue
        if r.get("tree_id") is not None \
                and r["tree_id"] in state.get("dead_trees", ()):
            # the draw landed on a tree other rigs already starved
            # on -- but the pool's global rotation reassigns the rig a
            # fresh tree under the SAME order, so waiting ~1 rotation
            # gets the redraw free: no creation fee, no risk of paying
            # for another dead offering, no hardware churn. Wait out
            # the dead segment (suppresses every kill rule below) and
            # cancel only if the rig is STILL on the same dead tree
            # past ~1.5 rotations -- the rotation evidently isn't
            # reaching it, so a real re-draw is warranted.
            life = state.get("tree_lifetime_min") or 20
            on_dead = (now - r["tree_since"]
                       if r.get("tree_since") is not None else None)
            if on_dead is not None \
                    and on_dead <= _minutes(1.5 * life):
                actions.append({
                    "type": "tree_wait", "order_id": r["order_id"],
                    "server_id": r["server_id"],
                    "tree_id": r["tree_id"],
                    "note": "on dead tree {}; waiting for rotation "
                            "(~{:.0f}m)".format(r["tree_id"], life)})
                continue
            actions.append({"type": "cancel", "order_id": r["order_id"],
                            "server_id": r["server_id"],
                            "tier": r["tier"], **_ctx(r),
                            "reason": "dead_tree", "rerent_at": None,
                            "retry_burst": _retry_burst(
                                r, now, life)
                                if role == "slot" else None})
            continue
        if t0 is None or obs_age is None \
                or obs_age > timedelta(seconds=cfg["stale_obs_sec"]):
            continue
        if _minutes(cfg["fault_check_min"]) <= r["age"] \
                < _minutes(cfg["free_window_min"]) and (
                r["last_restart_at"] is None or now - r[
                    "last_restart_at"] >= _minutes(
                        cfg["startup_restart_every_min"])):
            fault = _fault_reason(r)
            if fault:
                actions.append({
                    "type": "cancel", "order_id": r["order_id"],
                    "server_id": r["server_id"], "tier": r["tier"],
                    **_ctx(r),
                    "reason": f"fault:{fault}", "rerent_at": None})
                continue
        if role != "slot":
            continue
        if now - t0 < _minutes(cfg["startup_min"]):
            # startup grace: a fresh rig gets startup_min to land its
            # first share before any dry/noise rules apply
            continue
        vm = state.get("value_model") or {}
        predicted_h = None
        if usd_hr_fn is not None and r["its_total"] \
                and vm.get("S") and vm.get("its_ref") \
                and vm.get("beta") is not None:
            predicted_h = vm["S"] * (
                r["its_total"] / vm["its_ref"]) ** vm["beta"]

        def _realized_h(since):
            """Shares/hour among arrivals since `since`; the window
            can't start before the rig's first telemetry + warmup, or
            a rental younger than its x arm gets dilated by hours it
            never ran."""
            start = max(since, t0 + _minutes(2))
            hours = (now - start).total_seconds() / 3600
            if hours <= 0:
                return 0.0
            return sum(c for ts, c in r["share_arrivals"]
                       if ts >= start) / hours

        def _lam(since):
            """Expected-share mass since `since`: a healthy rig at
            predicted_h would have produced ~lam shares in the window,
            so P(none observed | healthy) ~= e^-lam. Verdicts wait for
            lam >= kill_lambda, making windows stretch with share
            scarcity rather than run a fixed clock."""
            if predicted_h is None:
                return None
            start = max(since, t0 + _minutes(2))
            hours = (now - start).total_seconds() / 3600
            return predicted_h * hours if hours > 0 else 0.0

        def _evidence_min(lam):
            """Hashing minutes needed for `lam` expected shares --
            the window that makes a share verdict a ~e^-lam
            false-kill."""
            return lam / predicted_h * 60 if predicted_h else None

        def _below_yield(since, frac=None):
            return predicted_h is not None and _realized_h(since) \
                < predicted_h * (frac if frac is not None
                                 else cfg["yield_floor_frac"])

        if predicted_h is not None \
                and r["age"] <= _minutes(cfg["free_window_min"]):
            # early yield gate: a draw either produces right away or
            # drips for hours -- inside the free window a bad tree is
            # cancelled free rather than ridden out. The verdict waits
            # for kill_lambda_free expected shares so a scarce-share
            # epoch can't turn it into a coin flip.
            if _below_yield(t0 + _minutes(2)) and (
                    _lam(t0) or 0) >= cfg["kill_lambda_free"]:
                actions.append({
                    "type": "cancel", "order_id": r["order_id"],
                    "server_id": r["server_id"], "tier": r["tier"],
                    **_ctx(r),
                    "reason": "low_yield", "rerent_at": None,
                    "retry_burst": _retry_burst(r, now,
                                state.get("tree_lifetime_min") or 20)})
                continue
        quiet = r["quiet_since"]
        reroll = r["reroll_start"]
        if reroll is not None and now - reroll >= _minutes(r["x_min"]) \
                and (_lam(reroll) is None
                     or _lam(reroll) >= cfg["kill_lambda"]):
            # a re-roll whose yield stayed below the floor for its x
            # arm -- or produced literally nothing: stop the burn; a
            # retry burst fills the slot after the y arm.
            post_reroll_dry = r["last_share_at"] is None \
                or r["last_share_at"] < reroll
            if post_reroll_dry or _below_yield(
                    reroll, cfg["yield_x_floor_frac"]):
                actions.append({
                    "type": "cancel", "order_id": r["order_id"],
                    "server_id": r["server_id"], "tier": r["tier"],
                    **_ctx(r),
                    "reason": "low_yield_after_x"
                    if not post_reroll_dry else "dry_after_x",
                    "rerent_at": None,
                    "retry_burst": _retry_burst(r, now,
                                state.get("tree_lifetime_min") or 20)})
                continue
        produced = r["last_share_at"] is not None
        # incumbent yield reads the widest of the x arm and the
        # kill_lambda evidence window: scarce-share epochs stretch
        # the window so a sub-floor verdict still means something
        win_min = max(r["x_min"], _evidence_min(cfg["kill_lambda"]) or 0)
        low_yield = predicted_h is not None and _below_yield(
            now - _minutes(win_min), cfg["yield_x_floor_frac"])
        if produced and (low_yield or (
                predicted_h is None
                and now - quiet >= _minutes(r["x_min"]))):
            # a below-floor incumbent rides out the burst: at judge it
            # is re-checked and only cancelled when a probe beats it or
            # nothing produced. x_min is the tolerance arm; without a
            # calibrated model the old silence rule is the fallback.
            held = state.get("drought_holds", {}).get(r["order_id"])
            if held is not None and now - held < _minutes(r["y_min"]):
                # drought reprieve still cooling: let the y arm pass
                # before re-auditing so the drought has room to break
                continue
            if burst_room > 0 and r["order_id"] not in busy_seats:
                actions.append({
                    "type": "burst", "trigger": "dry",
                    "trigger_order_id": r["order_id"],
                    "y_min": r["y_min"], "n": cfg["burst_max"]})
                burst_room -= 1
                busy_seats.add(r["order_id"])
            continue
        dry_min = cfg["dry_after_min"]
        if predicted_h is not None:
            # evidence first, patience second: wait for kill_lambda
            # expected shares (~e^-3 false-kill) but never ride a
            # silent draw past the longest x arm -- a rig expecting
            # under ~3 shares/hour isn't worth the doubt
            dry_min = max(dry_min, min(
                _evidence_min(cfg["kill_lambda"]) or 0,
                max(cfg["x_min"])))
        if not produced and now - quiet >= _minutes(dry_min):
            # never produced: nothing worth rechecking -- cancel
            # outright and let a retry burst fill the vacancy.
            actions.append({
                "type": "cancel", "order_id": r["order_id"],
                "server_id": r["server_id"], "tier": r["tier"],
                **_ctx(r),
                "reason": "dry", "rerent_at": None,
                "retry_burst": _retry_burst(r, now,
                                state.get("tree_lifetime_min") or 20)})
            continue

    # operator pins are audition demand, not just fill priority: a
    # pending pin that is currently rentable drives its own probe
    # burst rather than waiting for a qualifying upgrade scan
    if burst_room > 0 and (state.get("probe_pins") or []):
        fireable = [sid for sid in state["probe_pins"]
                    if sid in state["listings"]
                    and not state["listings"][sid].get("rented")
                    and sid not in state["excluded"]
                    and sid not in state["quote_rejected"]
                    and _upgrade_allowed(sid, pinned=True)]
        if fireable:
            actions.append({
                "type": "burst", "trigger": "pin",
                "trigger_order_id": None,
                "n": min(len(fireable), cfg["burst_max"])})
            burst_room -= 1

    if burst_room > 0 and state["burst_request_n"]:
        actions.append({"type": "burst", "trigger": "manual",
                        "trigger_order_id": None,
                        "n": state["burst_request_n"]})
        burst_room -= 1

    n_slots = sum(1 for r in state["rentals"] if r["role"] == "slot")
    # over-target contraction (dynamic mode can size the mandate
    # below the live fleet): the weakest excess slots are denied
    # renewal under no_renew so they exit at term end, or cancelled
    # outright under cull -- one below-average victim per cycle, so a
    # standout is never killed to meet the number. passive leaves
    # contraction to ordinary attrition. Unrankable rigs (fresh or
    # unmeasured) hold their seats but can't be picked as excess.
    excess_ids = set()
    if cfg["shrink_mode"] != "passive" \
            and n_slots > state["slot_target"]:
        _cancel_q = {a["order_id"] for a in actions
                     if a["type"] == "cancel"}
        _ranked = sorted(
            (r for r in state["rentals"]
             if r["role"] == "slot" and r["its_total"]
             and r["age"] >= _minutes(cfg["startup_min"])
             and r["order_id"] not in _cancel_q),
            key=_slot_score)
        _excess = _ranked[:n_slots - state["slot_target"]]
        if cfg["shrink_mode"] == "cull" and _excess:
            _mean = (sum(_slot_score(r) for r in _ranked)
                     / len(_ranked) if _ranked else None)
            victim = next(
                (r for r in _excess
                 if _mean is None or _slot_score(r) < _mean), None)
            if victim is not None:
                actions.append({
                    "type": "cancel", "order_id": victim["order_id"],
                    "server_id": victim["server_id"],
                    "tier": victim["tier"], **_ctx(victim),
                    "reason": "over_target", "rerent_at": None})
        else:
            excess_ids = {r["order_id"] for r in _excess}
    # term-end succession: audition replacements before the incumbent
    # expires so the slot never drops to a cold fill. The burst's own
    # judgement either promotes a proven winner or renews the
    # incumbent -- renewal is the fallback, not the default.
    if burst_room > 0:
        # arm early enough for the slowest audition a probe can run:
        # fill + warm-up + the spot judge window when spot probes are
        # possible -- a 20-min window only fits on-demand auditions
        succession_window = cfg["succession_min"]
        if cfg["spot_enabled"]:
            succession_window = max(
                succession_window,
                cfg["spot_probe_judge_min"] + cfg["burst_fill_min"] + 8)
        expiring = [
            r for r in state["rentals"]
            if r["role"] == "slot"
            and r.get("deadline_at") is not None
            and not r.get("auto_renew")
            and r["order_id"] not in state["succession_bursts"]
            and r["order_id"] not in busy_seats
            and r["order_id"] not in excess_ids
            and timedelta(0) <= r["deadline_at"] - now
                <= _minutes(succession_window)]
        # every expiring slot gets its own concurrent audition --
        # nearest deadline first when the burst cap binds
        for target in sorted(expiring,
                             key=lambda r: r["deadline_at"]):
            if burst_room <= 0:
                break
            actions.append({
                "type": "burst", "trigger": "succession",
                "trigger_order_id": target["order_id"],
                "n": cfg["burst_max"]})
            burst_room -= 1
            busy_seats.add(target["order_id"])
            if say:
                say(f"{_iso(now)} note succession burst: "
                    f"#{target['order_id']} deadline "
                    f"{_iso(target['deadline_at'])}")
    # opportunistic upgrade: a listing predicted to beat the weakest
    # slot on forecast net profit earns a one-probe audition (relative
    # value/$ remains the fallback without fresh dollar calibration).
    # Measured per-server history answers the hardware question when
    # it exists, model-class extrapolation otherwise; the probe still
    # has to verify its fresh tree draw actually produces. Worth the
    # audition when the fleet is full -- a vacant slot captures the
    # same upside through the normal fill -- or when a vacancy exists
    # but pace and budget block every ordinary fill: replacing the
    # incumbent's burn is then the only way to capture it.
    upgrade_scan = n_slots >= state["slot_target"]
    vacancy_blocked = False
    if not upgrade_scan and n_slots > 0 \
            and not state["pending_rerents"]:
        # a rerent reservation holds the vacancy for its own use;
        # only when nothing does and nothing can be normally filled
        # does the replacement audition run
        fill_ok = any(
            c["prior"] is not None and not _below_floor(c)
            and not _below_return(c)
            and c["eff_hr"] <= headroom_hr
            and _quote_fits(c, term, state["remaining"])
            for c in _candidates(
                state, cfg,
                {r["server_id"] for r in state["rentals"]}, [],
                term=term, spot_mode=cfg["spot_slot_mode"]))
        vacancy_blocked = not fill_ok
        upgrade_scan = vacancy_blocked
    if burst_room > 0 and upgrade_scan:
        # a slot already headed for a cancel this cycle is not an
        # upgrade target
        cancelling = {a["order_id"] for a in actions
                      if a.get("type") == "cancel"}
        # measured slots are displacement targets regardless of age:
        # startup grace protects rigs that haven't reported yet, not
        # rigs whose early evidence is already bad
        slots = [r for r in state["rentals"]
                 if r["role"] == "slot" and r["its_total"]
                 and r["order_id"] not in cancelling]
        if slots:
            worst = min(slots, key=_slot_score)
            worst_eff = _slot_score(worst)
            # the challenger would replace the incumbent's burn, so
            # pace allows headroom plus the incumbent's own hourly
            pace_cap = headroom_hr + float(worst["hourly_usd"])
            active_ids = {r["server_id"] for r in state["rentals"]}
            best, skipped_pace = None, None
            for c in _candidates(
                    state, cfg, active_ids, [], term=term,
                    spot_min_discount=cfg["spot_probe_min_discount"],
                    spot_mode=cfg["spot_probe_mode"]):
                sid = c["server_id"]
                if not _upgrade_allowed(sid):
                    continue
                if c["prior"] is None:
                    continue
                # never audition on prior-epoch numbers: a pool algo
                # change makes them a different unit
                if c.get("cross_epoch"):
                    continue
                if _below_floor(c) or _below_return(c):
                    continue
                if vacancy_blocked and (
                        _below_floor(c) or _below_return(c)
                        or c["eff_hr"] >= float(worst["hourly_usd"])
                        or not _quote_fits(
                            c, term, state["remaining"])):
                    # a replacement must clear the same gates the
                    # probe fill will apply, and stay cheaper than
                    # the incumbent burn it frees after the fresh
                    # fee is amortized -- otherwise the audition is
                    # doomed or buys nothing
                    continue
                if c["eff_hr"] > pace_cap:
                    if skipped_pace is None or (
                            _candidate_score(c) > _candidate_score(skipped_pace)):
                        skipped_pace = c
                    continue
                if best is None or _candidate_score(c) > _candidate_score(best):
                    best = c
            margin = max(0.0, worst_eff) * (1 + cfg["upgrade_margin"])
            if best is not None and _candidate_score(best) > margin \
                    and worst["order_id"] not in busy_seats:
                if _quote_fits(best, term,
                               state["remaining"]):
                    actions.append({
                        "type": "burst", "trigger": "upgrade",
                        "trigger_order_id": worst["order_id"],
                        "server_id": best["server_id"],
                        "y_min": worst["y_min"], "n": 1})
                    burst_room -= 1
                    if say:
                        unit = _score_unit()
                        context = ("; vacant slot blocked by pace/budget"
                                   if vacancy_blocked else "")
                        say(f"{_iso(now)} note upgrade scan: audition "
                            f"s{best['server_id']} est "
                            f"{_candidate_score(best):.4g} {unit} vs "
                            f"#{worst['order_id']} {worst_eff:.4g} "
                            f"(need >{margin:.4g}){context}")
                elif say:
                    say(f"{_iso(now)} note upgrade scan: s"
                        f"{best['server_id']} dominant but est "
                        f"${_est_bound(best, term):.2f} > "
                        f"remaining ${state['remaining']:.2f}")
            elif say and best is not None:
                unit = _score_unit()
                say(f"{_iso(now)} note upgrade scan: best s"
                    f"{best['server_id']} est {_candidate_score(best):.4g} "
                    f"{unit} vs worst {worst_eff:.4g} "
                    f"(need >{margin:.4g})")
            elif say and skipped_pace is not None \
                    and _candidate_score(skipped_pace) > margin:
                say(f"{_iso(now)} note upgrade scan: s"
                    f"{skipped_pace['server_id']} dominant but eff "
                    f"${skipped_pace['eff_hr']:.3f}/h > pace "
                    f"${pace_cap:.3f}/h")

    for pending in state["pending_bursts"]:
        due = pending["due_at"]
        if now > due + _minutes(cfg["rerent_grace_min"]):
            actions.append({"type": "retry_expired",
                            "for_order": pending["for_order"]})
            continue
        if now < due:
            continue
        if burst_room > 0:
            # a retry is a fresh-draw audition, not a reservation --
            # but when the vacated box was proven, it re-enters the
            # fill as the directed pick: the hardware already
            # measured good, the retry just buys it a new tree draw
            actions.append({
                "type": "burst", "trigger": "dry_retry",
                "trigger_order_id": pending["for_order"],
                "server_id": pending["server_id"]
                            if pending.get("proven") else None,
                "y_min": pending["y_min"], "n": cfg["burst_max"]})
            burst_room -= 1

    def _probe_evidence_min(p):
        """Hashing minutes before a probe's verdict counts: the
        judge floor, stretched to kill_lambda_free expected shares
        under the pool's current difficulty, bounded a minute short
        of the cancel deadline so on-demand probes never outlive
        their free window."""
        base = _probe_judge_min(p.get("mode"), cfg)
        vm = state.get("value_model") or {}
        its = p.get("its_total")
        if its and vm.get("S") and vm.get("its_ref") \
                and vm.get("beta") is not None:
            p_h = vm["S"] * (its / vm["its_ref"]) ** vm["beta"]
            if p_h > 0:
                need = cfg["kill_lambda_free"] / p_h * 60
                base = min(max(base, need),
                           _probe_cancel_min(p.get("mode"), cfg) - 1)
        return base

    def _matured(p):
        mode = p.get("mode")
        return p["ended"] or (
            p.get("t0") is not None
            and now - p["t0"]
            >= _minutes(_probe_evidence_min(p))) \
            or p["age"] >= _minutes(
                _probe_cancel_min(mode, cfg) - 1)

    def _found_share(p):
        return p.get("t0") is not None and any(
            ts >= p["t0"] for ts, _ in p.get("share_arrivals", []))

    # concurrent bursts can mature together, but at most one verdict
    # lands per cycle: each commits against vacancy math the next
    # mature burst must see fresh -- it judges on the following cycle
    burst = None
    probes = []
    judge_now = False
    for _b in state["unjudged_bursts"]:
        _probes = state["burst_probes"].get(_b["burst_id"], [])
        if _probes:
            # probes fill within one cycle now, so their deadlines
            # land together: judge once every probe has ended or had
            # its probe_judge_min of hashing, or when a proven
            # finder is about to hit its free-window deadline
            judge_now = all(_matured(p) for p in _probes) or any(
                not p["ended"] and p["role"] == "probe"
                and _found_share(p)
                and p["age"] >= _minutes(
                    _probe_cancel_min(p.get("mode"), cfg) - 1)
                for p in _probes)
        else:
            judge_now = now - _b["started_at"] >= _minutes(
                cfg["burst_fill_min"])
        # a burst whose probes never mature can't wait forever
        stall_min = cfg["burst_fill_min"] + max(
            (_probe_cancel_min(p.get("mode"), cfg)
             + _probe_judge_min(p.get("mode"), cfg)
             for p in _probes),
            default=cfg["probe_cancel_by_min"]
            + cfg["probe_judge_min"])
        judge_now = judge_now or now - _b["started_at"] >= \
            _minutes(stall_min)
        if _b["trigger"] == "succession":
            # the renewal fallback is refused inside the final minute
            # of a term, so the judgement can't dawdle past deadline-3;
            # an already-expired original judges immediately
            trig = next(
                (r for r in state["rentals"]
                 if r["order_id"] == _b["trigger_order_id"]), None)
            judge_now = judge_now or trig is None or (
                trig.get("deadline_at") is not None
                and now >= trig["deadline_at"] - _minutes(3))
        if judge_now:
            burst, probes = _b, _probes
            break
    judge_emitted = False
    if burst is not None:
        if judge_now:
            finder_ids = {p["order_id"] for p in probes
                          if _found_share(p)}
            # probes at/past the deadline are left to the probe_deadline
            # cancel; they can't be promoted or judged-cancelled.
            live = [p for p in probes
                    if not p["ended"] and p["role"] == "probe"
                    and p["age"] < _minutes(
                        _probe_cancel_min(p.get("mode"), cfg))]
            finders = [p for p in live if p["order_id"] in finder_ids]
            original = next(
                (r for r in state["rentals"]
                 if r["order_id"] == burst["trigger_order_id"]
                 and r["role"] == "slot"), None)
            # a share during the burst window means the incumbent's
            # tree rotated or warmed up: it competes as a candidate.
            recovered = original is not None and any(
                ts >= burst["started_at"]
                for ts, _ in original["share_arrivals"])

            def _probe_term_h(p):
                """Hours left on the probe's own term, bounded by the
                phase and epoch ends: the window its keep-decision
                economics are judged over."""
                if p.get("deadline_at") is None:
                    return term
                bound = min(b for b in (
                    p["deadline_at"], state["phase_ends_at"],
                    state["epoch_ends_at"]) if b is not None)
                return max(
                    0.0, (bound - now).total_seconds() / 3600)

            def _ret(p):
                if usd_hr_fn is None:
                    return None
                revenue = _realized_usd_h(p)
                if revenue is None:
                    revenue = usd_hr_fn(p["its_total"])
                return predicted_return(
                    revenue, p["hourly_usd"], _probe_term_h(p),
                    _dec(p.get("creation_fee_usd"),
                         str(_CREATION_FEE_USD)))

            def _econ(p):
                """A finder only earns promotion when its measured
                return clears min_return over its remaining term,
                fresh creation fee included. Realized yield leads:
                the audition exists to measure the draw, so a fast
                probe that produced little is not economic no matter
                what its speed forecasts."""
                if cfg["loss_mining"] or usd_hr_fn is None:
                    return True
                ret = _ret(p)
                return ret is not None \
                    and ret >= cfg["min_return"]

            def _seat_econ(p):
                """Seat floor that survives loss_mining: a seat is a
                standing cost commitment, so even when _econ waves
                every finder through, a probe only earns a NEW seat
                when its return clears min_seat_return."""
                if usd_hr_fn is None:
                    return True
                ret = _ret(p)
                return ret is not None \
                    and ret >= cfg["min_seat_return"]

            # finder_ids keeps every true finder for logging and drop
            # reasons; only economic finders can be promoted
            econ_finders = [p for p in finders if _econ(p)]

            def _gross_revenue(p):
                """Gross yardstick for probes and incumbents alike:
                realized yield where the audition/grace window makes
                it real, else the forecast revenue; the its-proxy
                value without the model. Cost never enters."""
                if usd_hr_fn is None:
                    return _its_value(p["its_total"])
                revenue = None
                if p.get("role") == "probe" \
                        or (p.get("age") or timedelta(0)) >= _minutes(
                            cfg["actual_yield_min"]):
                    revenue = _realized_usd_h(p)
                if revenue is None:
                    revenue = usd_hr_fn(p["its_total"])
                return revenue

            def _eff(p):
                if cfg["score"] == "its":
                    return p["its_total"] or 0.0
                if cfg["score"] == "gross":
                    v = _gross_revenue(p)
                    return float(v) if v is not None else 0.0
                hourly = float(p["hourly_usd"])
                if usd_hr_fn is not None:
                    if p.get("role") == "probe":
                        ret = predicted_return(
                            usd_hr_fn(p["its_total"]), hourly,
                            _probe_term_h(p),
                            _dec(p.get("creation_fee_usd"),
                                 str(_CREATION_FEE_USD)))
                        return ret or 0.0
                    revenue = usd_hr_fn(p["its_total"])
                    return (float(revenue) / hourly) \
                        if revenue is not None and hourly else 0.0
                if p.get("role") == "probe":
                    # a swap costs the challenger a fresh creation
                    # fee; amortize it over the term. The incumbent's
                    # fee is already sunk.
                    hourly += float(_dec(
                        p.get("creation_fee_usd"),
                        str(_CREATION_FEE_USD))) / term
                its = p["its_total"] or 0
                return (_its_value(its) or 0.0) / hourly \
                    if hourly else 0.0

            def _rank(p, metric=None):
                metric = metric or cfg["score"]
                if metric == "its":
                    return p["its_total"] or 0.0
                if metric == "gross":
                    v = _gross_revenue(p)
                    return (float(v) if v is not None
                            else -float(p["hourly_usd"]))
                if usd_hr_fn is None:
                    return _eff(p)
                revenue = None
                if p.get("role") == "probe":
                    # probes are judged on the draw they just
                    # auditioned: realized yield, forecast only while
                    # telemetry is too thin to price
                    revenue = _realized_usd_h(p)
                elif p.get("role") == "slot" and (p.get("age")
                        or timedelta(0)) >= _minutes(
                        cfg["actual_yield_min"]):
                    revenue = _realized_usd_h(p)
                if revenue is None:
                    revenue = usd_hr_fn(p["its_total"])
                profit = forecast_profit(
                    revenue, p["hourly_usd"],
                    _probe_term_h(p) if p.get("role") == "probe"
                    else None,
                    _dec(p.get("creation_fee_usd"),
                         str(_CREATION_FEE_USD)))
                return profit if profit is not None else -float(p["hourly_usd"])

            def _gross_close(a, b):
                """Revenue near-tie: both positive and within
                gross_tiebreak of the larger. No positive gross means
                there is nothing to arbitrate."""
                if a <= 0 or b <= 0:
                    return False
                return min(a, b) >= max(a, b) * (
                    1 - cfg["gross_tiebreak"])

            def _rank_max(cands):
                """Highest score; under score=gross a revenue
                near-tie among the leaders is arbitrated by net --
                same gross but much better economics takes it."""
                top = max(cands, key=_rank, default=None)
                if top is None or cfg["score"] != "gross" \
                        or _rank(top) <= 0:
                    return top
                close = [p for p in cands
                         if _gross_close(_rank(p), _rank(top))]
                return max(close, key=lambda p: _rank(p, "net"))

            def _dominates(c, o, margin):
                """Margin test on the score. Under gross a revenue
                near-tie is arbitrated on net instead: the cheaper
                earner of the same gross takes the contest."""
                if cfg["score"] == "gross" and _gross_close(
                        _rank(c), _rank(o)):
                    return (_rank(c, "net")
                            > max(0.0, _rank(o, "net")) * (1 + margin))
                return (_rank(c)
                        > max(0.0, _rank(o)) * (1 + margin))

            def _pick(cands):
                """Keep the speed floor, then rank by the configured
                score (calibrated net profit, gross revenue or it/s;
                relative efficiency without calibration)."""
                top = max(p["its_total"] or 0 for p in cands)
                pool = [p for p in cands if (p["its_total"] or 0)
                        >= cfg["promote_its_floor"] * top]
                return _rank_max(pool)

            def _drop(p):
                return {"order_id": p["order_id"],
                        "server_id": p["server_id"],
                        "tier": p.get("tier"),
                        "its": p.get("its_total"),
                        "shares": sum(c for _, c in
                                      p.get("share_arrivals", [])),
                        "score_med": p.get("score_med"),
                        "score_per_share": p.get("score_per_share"),
                        "age_min": int(p["age"].total_seconds() // 60),
                        "reason": ("probe_lost"
                                   if p["order_id"] in finder_ids
                                   else "probe_no_share")}

            judge = {"type": "judge", "burst_id": burst["burst_id"],
                     "finders": sorted(finder_ids), "promote": None,
                     "promote_extra": [], "cancel_original": None,
                     "retry": None, "cancel": [], "verdict": None}

            def _orig_cancel(reason):
                return {"order_id": original["order_id"],
                        "server_id": original["server_id"],
                        "tier": original["tier"], "reason": reason,
                        **_ctx(original)}
            cancel_queued = {a["order_id"] for a in actions
                             if a["type"] == "cancel"}

            def _seat_bar():
                """Growth bar for a NEW seat: the score of the weakest
                rankable slot. A probe better than the fleet's worst
                measured member earns a seat alongside -- a single
                standout can no longer pull the whole mean out of
                reach and veto all expansion. None when no rankable
                fleet exists to measure against."""
                slots = [r for r in state["rentals"]
                         if r["role"] == "slot" and r["its_total"]
                         and r["order_id"] not in cancel_queued]
                if not slots:
                    return None
                return min(_rank(r) for r in slots)

            def _tree_miss(p, orig_eff):
                """Only the share is missing -- hardware and
                economics both measured good."""
                if p["order_id"] in finder_ids \
                        or p["order_id"] in cancel_queued:
                    return False
                if p.get("t0") is None or now - p["t0"] < \
                        _minutes(_probe_judge_min(p.get("mode"), cfg)):
                    return False
                obs_age = p.get("obs_age")
                # future telemetry is not healthy evidence
                if not isinstance(obs_age, timedelta) \
                        or obs_age > timedelta(
                            seconds=cfg["stale_obs_sec"]) \
                        or obs_age < timedelta(0):
                    return False
                if p.get("recent_errors_only"):
                    return False
                its, gpu = (p.get("its_total"), p.get("gpu_its"))
                if not (isinstance(its, (int, float))
                        and not isinstance(its, bool)
                        and math.isfinite(its) and its > 0):
                    return False
                if not (isinstance(gpu, (int, float))
                        and not isinstance(gpu, bool)
                        and math.isfinite(gpu) and gpu > 0):
                    return False
                if p.get("listing_gpu_count") != 1 \
                        or p.get("gpu_count_seen") != 1:
                    return False
                if _fault_reason(p) is not None:
                    return False
                if not _econ(p):
                    return False
                eff = _eff(p)
                if _rank(p) <= max(0.0, orig_eff) * (
                        1 + cfg["upgrade_margin"]):
                    return False
                if not cfg["loss_mining"] and value_floor is not None \
                        and eff < value_floor:
                    return False
                return True

            if original is not None and burst["trigger"] == "succession":
                # the incumbent hits its term end regardless: only a
                # challenger that clearly out-earns it takes over now;
                # anything less keeps the proven rig alive on
                # auto-renew rather than dropping to a cold vacancy.
                # fee-inclusive on the challenger, sunk fee on the
                # incumbent -- same economics as an upgrade judgement
                orig_eff = _rank(original)
                winner = _rank_max(econ_finders)
                winner_eff = (_rank(winner)
                              if winner is not None else None)
                stat_probe = max(finders, key=_rank, default=None)
                judge["stats"] = {
                    "orig_its": original["its_total"],
                    "orig_value_per_usd": round(_eff(original), 1),
                    "metric": ({"net": "net_usd_hr",
                                "gross": "gross_usd_hr",
                                "its": "its_per_s"}[cfg["score"]]
                               if usd_hr_fn is not None
                               or cfg["score"] == "its"
                               else "value_per_usd"),
                    "orig_net_usd_hr": (
                        orig_eff if usd_hr_fn is not None else None),
                    "probe_net_usd_hr": (
                        (_rank(stat_probe)
                         if stat_probe is not None else None)
                        if usd_hr_fn is not None else None),
                    "probe_its": (stat_probe["its_total"]
                                  if stat_probe else None),
                    "probe_value_per_usd": (
                        round(_eff(stat_probe), 1)
                        if stat_probe is not None else None),
                    "tiebreak": (
                        "net" if cfg["score"] == "gross"
                        and winner is not None
                        and _gross_close(_rank(winner), orig_eff)
                        else None)}
                if winner is not None and _dominates(
                        winner, original, cfg["upgrade_margin"]):
                    judge["promote"] = winner["order_id"]
                    judge["cancel_original"] = _orig_cancel("upgraded")
                    judge["cancel"] = [
                        _drop(p) for p in live
                        if p["order_id"] != winner["order_id"]]
                    judge["verdict"] = "upgraded"
                else:
                    judge["cancel"] = [_drop(p) for p in live]
                    if original.get("auto_renew"):
                        judge["verdict"] = "kept_original"
                    else:
                        # auto-renew stays off as a strategy: renew by
                        # re-renting the same server when its term
                        # ends -- the rerent gates at that moment
                        # refuse it if a reprice stopped clearing the
                        # return gate
                        judge["renew"] = {
                            "order_id": original["order_id"],
                            "server_id": original["server_id"],
                            "tier": original["tier"],
                            "due_at": (
                                _iso(original["deadline_at"])
                                if original.get("deadline_at")
                                is not None else None)}
                        judge["verdict"] = "renewed"
            elif original is not None and (
                    recovered or burst["trigger"] in (
                        "upgrade", "reactivated")):
                if burst["trigger"] in ("upgrade", "reactivated"):
                    # an upgrade burst never dry-cancels the incumbent:
                    # a challenger promotes only by beating its
                    # measured forecast score by the margin -- fee-inclusive
                    # on the challenger, sunk fee on the incumbent. A
                    # reactivation burst plays the same game against
                    # the weakest slot: the incumbent isn't on trial
                    # for the foreign probe's failure.
                    orig_eff = _rank(original)
                    winner = _rank_max(econ_finders)
                    winner_eff = (_rank(winner)
                                  if winner is not None else None)
                    stat_probe = max(finders, key=_rank, default=None)
                    judge["stats"] = {
                        "orig_its": original["its_total"],
                        "orig_value_per_usd": round(_eff(original), 1),
                        "metric": ("net_usd_hr" if usd_hr_fn is not None
                                   else "value_per_usd"),
                        "orig_net_usd_hr": (
                            orig_eff if usd_hr_fn is not None else None),
                        "probe_net_usd_hr": (
                            (_rank(stat_probe)
                             if stat_probe is not None else None)
                            if usd_hr_fn is not None else None),
                        "probe_its": (stat_probe["its_total"]
                                      if stat_probe else None),
                        "probe_value_per_usd": (
                            round(_eff(stat_probe), 1)
                            if stat_probe is not None else None),
                        "tiebreak": (
                            "net" if cfg["score"] == "gross"
                            and winner is not None
                            and _gross_close(_rank(winner), orig_eff)
                            else None)}
                    seat_bar = _seat_bar()
                    judge["stats"]["seat_bar"] = (
                        round(seat_bar, 4)
                        if seat_bar is not None else None)
                    if winner is not None and seat_bar is not None \
                            and n_slots < cfg["max_rentals"] \
                            and _rank(winner) > seat_bar \
                            and _seat_econ(winner):
                        # a clear tier above the whole fleet earns a
                        # NEW seat alongside the incumbent rather than
                        # taking the weakest one's
                        judge["promote"] = winner["order_id"]
                        judge["cancel"] = [
                            _drop(p) for p in live
                            if p["order_id"] != winner["order_id"]]
                        judge["verdict"] = "earned_seat"
                    elif winner is not None and _dominates(
                            winner, original, cfg["upgrade_margin"]):
                        judge["promote"] = winner["order_id"]
                        judge["cancel_original"] = _orig_cancel(
                            "upgraded")
                        judge["cancel"] = [
                            _drop(p) for p in live
                            if p["order_id"] != winner["order_id"]]
                        judge["verdict"] = "upgraded"
                    else:
                        judge["cancel"] = [_drop(p) for p in live]
                        judge["verdict"] = "kept_original"
                else:
                    winner = _pick(econ_finders + [original])
                    judge["stats"] = {
                        "orig_value_per_usd": round(_eff(original), 1),
                        "winner_value_per_usd": round(_eff(winner), 1),
                        "winner": winner["order_id"],
                        "winner_is_orig":
                            winner["order_id"] == original["order_id"],
                        "tiebreak": (
                            "net" if cfg["score"] == "gross"
                            and _gross_close(_rank(winner),
                                             _rank(original))
                            else None)}
                    seat_bar = _seat_bar()
                    judge["stats"]["seat_bar"] = (
                        round(seat_bar, 4)
                        if seat_bar is not None else None)
                    if winner["order_id"] == original["order_id"]:
                        judge["cancel"] = [_drop(p) for p in live]
                        judge["verdict"] = "kept_original"
                    elif seat_bar is not None \
                            and n_slots < cfg["max_rentals"] \
                            and _rank(winner) > seat_bar \
                            and _seat_econ(winner):
                        # exceptional vs the fleet mean: earns a NEW
                        # seat alongside the incumbent
                        judge["promote"] = winner["order_id"]
                        judge["cancel"] = [
                            _drop(p) for p in live
                            if p["order_id"] != winner["order_id"]]
                        judge["verdict"] = "earned_seat"
                    elif not _dominates(winner, original,
                                        cfg["promote_margin"]):
                        judge["cancel"] = [_drop(p) for p in live]
                        judge["verdict"] = "kept_original"
                    else:
                        judge["promote"] = winner["order_id"]
                        judge["cancel_original"] = _orig_cancel(
                            "replaced")
                        judge["cancel"] = [
                            _drop(p) for p in live
                            if p["order_id"] != winner["order_id"]]
                        judge["verdict"] = "promoted"
            elif original is not None:
                if econ_finders:
                    winner = _pick(econ_finders)
                    judge["promote"] = winner["order_id"]
                    judge["cancel_original"] = _orig_cancel("replaced")
                    judge["cancel"] = [
                        _drop(p) for p in live
                        if p["order_id"] != winner["order_id"]]
                    judge["verdict"] = "promoted"
                else:
                    its = original.get("its_total")
                    alive = isinstance(its, (int, float)) \
                        and math.isfinite(its) and its > 0
                    if original.get("share_arrivals") and alive \
                            and original["order_id"] not in \
                            state.get("drought_holds", {}):
                        # the incumbent already produced this order
                        # and is still hashing -- a totally silent
                        # audition is a crediting/tree drought, not
                        # dead hardware. Hold it once; the next
                        # low-yield audit still kills if the quiet
                        # persists.
                        judge["cancel"] = [_drop(p) for p in live]
                        judge["verdict"] = "drought_hold"
                    else:
                        # nothing produced anywhere; stop the burn and
                        # retry the vacancy after the slot's y arm
                        judge["cancel_original"] = _orig_cancel("dry")
                        judge["cancel"] = [_drop(p) for p in live]
                        judge["retry"] = _retry_burst(original, now,
                                state.get("tree_lifetime_min") or 20)
                        judge["verdict"] = "dry_retry"
            elif (seat_fillers := [
                        p for p in econ_finders if _seat_econ(p)]) \
                    and n_slots < state["slot_target"]:
                winner = _pick(seat_fillers)
                judge["promote"] = winner["order_id"]
                judge["cancel"] = [
                    _drop(p) for p in live
                    if p["order_id"] != winner["order_id"]]
                judge["verdict"] = "promoted"
            elif burst["trigger"] == "dry_retry" \
                    and n_slots < state["slot_target"]:
                # nobody produced: re-arm the vacancy for another y
                # arm of pool-rotation patience
                retry_y = burst["y_min"] or cfg["y_min"][0]
                judge["cancel"] = [_drop(p) for p in live]
                judge["retry"] = {
                    "for_order": burst["trigger_order_id"],
                    "server_id": None, "tier": None,
                    "y_min": retry_y,
                    "due_at": _iso(now + _minutes(retry_y))}
                judge["verdict"] = "retry_again"
            elif econ_finders and n_slots >= state["slot_target"]:
                # no open vacancy: the finder still earns a slot by
                # displacing the weakest incumbent through the same
                # margin economics an upgrade judgement applies
                winner = _pick(econ_finders)
                incumbent = min(
                    (r for r in state["rentals"]
                     if r["role"] == "slot" and r["its_total"]
                     and r["order_id"] not in cancel_queued),
                    key=_rank, default=None)
                if incumbent is not None:
                    orig_eff = _rank(incumbent)
                    stat_probe = max(finders, key=_rank, default=None)
                    judge["stats"] = {
                        "orig_its": incumbent["its_total"],
                        "orig_value_per_usd": round(
                            _eff(incumbent), 1),
                        "metric": (
                            {"net": "net_usd_hr",
                             "gross": "gross_usd_hr",
                             "its": "its_per_s"}[cfg["score"]]
                            if usd_hr_fn is not None
                            or cfg["score"] == "its"
                            else "value_per_usd"),
                        "orig_net_usd_hr": (
                            orig_eff
                            if usd_hr_fn is not None else None),
                        "probe_net_usd_hr": (
                            (_rank(stat_probe)
                             if stat_probe is not None else None)
                            if usd_hr_fn is not None else None),
                        "probe_its": (stat_probe["its_total"]
                                      if stat_probe else None),
                        "probe_value_per_usd": (
                            round(_eff(stat_probe), 1)
                            if stat_probe is not None else None),
                        "tiebreak": (
                            "net" if cfg["score"] == "gross"
                            and winner is not None
                            and _gross_close(_rank(winner), orig_eff)
                            else None)}
                seat_bar = _seat_bar()
                if incumbent is not None:
                    judge["stats"]["seat_bar"] = (
                        round(seat_bar, 4)
                        if seat_bar is not None else None)
                if seat_bar is not None \
                        and n_slots < cfg["max_rentals"] \
                        and _rank(winner) > seat_bar \
                        and _seat_econ(winner):
                    # exceptional vs the fleet mean: earns a NEW seat
                    # rather than displacing the weakest incumbent
                    judge["promote"] = winner["order_id"]
                    judge["cancel"] = [
                        _drop(p) for p in live
                        if p["order_id"] != winner["order_id"]]
                    judge["verdict"] = "earned_seat"
                elif incumbent is not None and _dominates(
                        winner, incumbent, cfg["upgrade_margin"]):
                    judge["promote"] = winner["order_id"]
                    judge["cancel"] = [
                        _drop(p) for p in live
                        if p["order_id"] != winner["order_id"]] + [{
                            "order_id": incumbent["order_id"],
                            "server_id": incumbent["server_id"],
                            "tier": incumbent["tier"],
                            "reason": "displaced",
                            **_ctx(incumbent)}]
                    judge["verdict"] = "displaced"
                else:
                    judge["cancel"] = [_drop(p) for p in live]
                    judge["verdict"] = "no_vacancy"
            else:
                judge["cancel"] = [_drop(p) for p in live]
                judge["verdict"] = ("no_vacancy" if finders
                                    else "no_finders")

            # Beyond the headline verdict: leftover economic finders
            # still earn seats rather than dying in the free window --
            # but every keep must beat something. An open mandated
            # seat takes a probe that outranks the weakest incumbent;
            # a contested seat takes a clear upgrade-margin win (the
            # incumbent is displaced); and growth into headroom under
            # max_rentals takes a probe that outranks the weakest seat
            # already held -- a single standout rig can't veto all
            # expansion the way the fleet-mean bar let it.
            extras = sorted(
                (p for p in econ_finders
                 if p["order_id"] != judge["promote"]),
                key=_rank, reverse=True)
            if extras:
                orig_out = (judge["cancel_original"]
                            or {}).get("order_id")
                judge_out = {c["order_id"] for c in judge["cancel"]}
                seated = sum(
                    1 for r in state["rentals"]
                    if r["role"] == "slot"
                    and r["order_id"] not in cancel_queued
                    and r["order_id"] not in judge_out
                    and r["order_id"] != orig_out) \
                    + (judge["promote"] is not None)
                incumbents = {
                    r["order_id"]: r for r in state["rentals"]
                    if r["role"] == "slot" and r["its_total"]
                    and r["order_id"] not in cancel_queued
                    and r["order_id"] not in judge_out
                    and r["order_id"] != orig_out}
                in_ranks = {o: _rank(r)
                            for o, r in incumbents.items()}
                new_ranks = []
                winner_probe = next(
                    (p for p in econ_finders
                     if p["order_id"] == judge["promote"]), None)
                if winner_probe is not None:
                    new_ranks.append(_rank(winner_probe))
                kept = set()
                for p in extras:
                    weakest = min(
                        incumbents.values(), key=_rank, default=None)
                    if seated < state["slot_target"] and (
                            weakest is None
                            or _rank(p) > _rank(weakest)) \
                            and _seat_econ(p):
                        via = "vacancy"
                    elif seated < cfg["max_rentals"] and (
                            not in_ranks and not new_ranks
                            or _rank(p) > min(
                                [*in_ranks.values(), *new_ranks])) \
                            and _seat_econ(p):
                        via = "growth"
                    elif weakest is not None and _dominates(
                            p, weakest, cfg["upgrade_margin"]):
                        via = "displace"
                    else:
                        continue
                    judge["promote_extra"].append({
                        "order_id": p["order_id"],
                        "server_id": p["server_id"],
                        "tier": p.get("tier"), "via": via})
                    kept.add(p["order_id"])
                    if via == "displace":
                        del incumbents[weakest["order_id"]]
                        del in_ranks[weakest["order_id"]]
                        judge["cancel"].append({
                            "order_id": weakest["order_id"],
                            "server_id": weakest["server_id"],
                            "tier": weakest["tier"],
                            "reason": "displaced", **_ctx(weakest)})
                    else:
                        seated += 1
                    new_ranks.append(_rank(p))
                if kept:
                    judge["cancel"] = [
                        c for c in judge["cancel"]
                        if c["order_id"] not in kept]
            # every cancelled probe that measured healthy but
            # shareless is a possible tree-luck miss, not a proven
            # hardware verdict: tag it for a bounded re-audition
            # (upgrade_tree_max_attempts, upgrade_cooldown spacing).
            # The tag bar is hardware health only -- positive it/s
            # on fresh obs, no fault, econ-passing; contention vs
            # incumbents is a seat question, not a retry one.
            cancel_ids = {c["order_id"] for c in judge["cancel"]}
            judge["tree_retry_order_ids"] = [
                p["order_id"] for p in live
                if p["order_id"] in cancel_ids
                and _tree_miss(p, 0.0)]
            actions.append(judge)
            judge_emitted = True

    rent_actions = []
    lull = state["lull_until"] is not None \
        and now < state["lull_until"]
    if lull:
        actions.append({
            "type": "lull",
            "streak": state["lull_streak"],
            "until": _iso(state["lull_until"]),
            "note": "consecutive yield failures; slot fills paused "
                    "-- probes still run, they die free"})
    if not blocks and now < state["phase_ends_at"]:
        active_ids = {r["server_id"] for r in state["rentals"]}
        chosen = []
        n_chosen_slots = 0
        rerented_ids = set()
        # one diversity-motivated pick per cycle at most: cross-
        # section sampling is a slow drip, not a per-burst flood
        novel_picked = False
        # every unjudged burst fills its probes in the SAME cycle: the
        # creates still run back-to-back, but without a cycle gap and
        # without serializing behind each other's SSH provision wait
        # (deferred provisioning) -- staggered fills gave late probes
        # a fraction of the audition window. Never in the cycle that
        # judged it, or the new probe is an orphan the judge never
        # saw. Concurrent bursts share the one global rental cap.
        judged_burst_id = burst["burst_id"] if judge_emitted else None
        # when the scout's fresh report shows the pool's current
        # offering is a proven-dead tree, a probe rented now is a
        # wasted draw -- it lands on the same assignment and dies
        # free, but spends a slot and its discoverable server on
        # nothing. Probes wait for the rotation (~life_min) instead;
        # the wait self-bounds since the scout's tree changes every
        # rotation. Slot fills are exempt: a vacancy's production
        # loss outweighs the bad-draw risk.
        agent = state.get("agent") or {}
        offering_dead = bool(agent) \
            and agent.get("tree_id") in state["dead_trees"]
        if offering_dead:
            actions.append({
                "type": "offering_dead",
                "tree_id": agent.get("tree_id"),
                "source": agent.get("source") or "agent",
                "note": "probe fills deferred: scout {} on dead tree "
                        "{} (rotation ~{:.0f}m)".format(
                            agent.get("source") or "agent",
                            agent.get("tree_id"),
                            state["tree_lifetime_min"])})
        for burst in state["unjudged_bursts"]:
            if burst["burst_id"] == judged_burst_id \
                    or offering_dead \
                    or now - burst["started_at"] >= _minutes(
                        cfg["burst_fill_min"]):
                continue
            n_open = sum(1 for p in state["burst_probes"].get(
                burst["burst_id"], []) if not p["ended"])
            # probes are prospective slots placed one per cycle: a
            # quote that stalls or a rig that never provisions still
            # leaves the burst fill_min window to pick the next one
            needed = min(burst["n_target"] - n_open, 1)
            if needed > 0 and len(state["rentals"]) + len(chosen) < \
                    cfg["max_rentals"] + cfg["burst_max"]:
                # probes are prospective slots, not lottery filler:
                # same ranking and term as a slot fill. The probe
                # auditions for the incumbent's slot, so pace gives
                # it headroom plus the incumbent's own burn -- each
                # pick spends down that allowance and the remaining
                # quote budget so the burst can't commit past either.
                rejects = {}
                probes_now = _candidates(
                    state, cfg, active_ids, chosen, rejects, term=term,
                    spot_min_discount=cfg["spot_probe_min_discount"],
                    spot_mode=cfg["spot_probe_mode"])
                incumbent = next(
                    (r for r in state["rentals"]
                     if r["order_id"] == burst["trigger_order_id"]
                     and r["role"] == "slot"), None)
                allowance = headroom_hr + (
                    float(incumbent["hourly_usd"])
                    if incumbent is not None else 0.0)
                target = burst.get("target_server")
                budget_left = state["remaining"]
                picks, over_budget, relaxed = [], [], []

                def _admit(c, ungated):
                    """Probe admission in gate order; returns the
                    reject reason or None. Ungated drops the score
                    gates; a pin additionally skips pace -- an
                    operator tag is an explicit spend decision.
                    History and the quote budget still bind everyone."""
                    # the retry's directed box carries its own
                    # warrant: the pending burst already spaced the
                    # re-audition, so tried/cooldown don't re-gate it
                    if not _upgrade_allowed(
                            c["server_id"],
                            pinned=c["server_id"] in pin_rank
                            or (c["server_id"] == target
                                and burst.get("trigger")
                                == "dry_retry")):
                        return "upgrade_history"
                    if not ungated:
                        if _below_floor(c):
                            return "below_floor"
                        if _below_return(c):
                            return "below_return"
                    if c["eff_hr"] > allowance \
                            and c["server_id"] not in pin_rank:
                        return "over_pace"
                    if not _quote_fits(c, term, budget_left):
                        return "over_budget"
                    return None

                # operator pins audition ahead of every scored pick,
                # in tag order; the algo fills whatever seats remain.
                # Pins skip the floor/return score gates and pace (an
                # operator tag is an explicit spend decision), but
                # history and the quote budget still bind (a quote
                # that can't fit must not blind-rent)
                pin_rank = {sid: i for i, sid in enumerate(
                    state.get("probe_pins") or [])}
                pin_reasons = {}
                # diversity is a bootstrap, not a quota: a class with
                # fewer than gpu_trial_min phase samples outranks
                # higher-forecast picks so priors get a cross-section
                # of actuals; at the threshold it competes on score
                # alone. Picks chosen this tick count too so
                # concurrent bursts don't dogpile one class. The tier
                # is not blind optimism though -- a class already
                # measured far below the leader has said its piece,
                # so only unmeasured or plausibly-competitive classes
                # get the boost.
                trials = dict(state.get("gpu_trials") or {})
                for c in chosen:
                    g = c.get("gpu") or ""
                    trials[g] = trials.get(g, 0) + 1
                gpu_detail = state["stats"].get("gpu_detail") or {}
                best_prior = max(
                    (d.get("prior") or 0
                     for d in gpu_detail.values()
                     if not d.get("cross_epoch")), default=0)

                def _novel(c):
                    if novel_picked:
                        return False
                    d = gpu_detail.get(c["gpu"] or "") or {}
                    return trials.get(c["gpu"] or "", 0) \
                        < cfg["gpu_trial_min"] and (
                            d.get("prior") is None
                            or d.get("cross_epoch")
                            or d["prior"] >= 0.15 * best_prior)
                order = sorted(
                    probes_now,
                    key=lambda c: (
                        pin_rank.get(c["server_id"], len(pin_rank) + 1),
                        0 if c["server_id"] == target else 1,
                        0 if _novel(c) else 1,
                        -_candidate_score(c)))
                novel_ids = {c["server_id"] for c in order
                             if _novel(c)}
                for c in order:
                    if len(state["rentals"]) + len(chosen) \
                            + len(picks) >= \
                            cfg["max_rentals"] + cfg["burst_max"]:
                        break
                    why = _admit(c, ungated=c["server_id"] in pin_rank)
                    if why == "over_budget":
                        over_budget.append(c)
                        if c["server_id"] in pin_rank:
                            pin_reasons[c["server_id"]] = why
                        continue
                    if why:
                        rejects[why] = rejects.get(why, 0) + 1
                        if c["server_id"] in pin_rank:
                            pin_reasons[c["server_id"]] = why
                        if cfg["loss_mining"] and why in (
                                "below_floor", "below_return"):
                            relaxed.append(c)
                        continue
                    if len(picks) >= needed:
                        continue
                    if c["server_id"] in pin_rank:
                        c["pinned"] = True
                    if cfg["loss_mining"] and _gate_violation(c):
                        c["loss_mining"] = True
                    picks.append(c)
                    # an algo-chosen diversity pick spends the
                    # cycle's one novelty slot; pin/target picks are
                    # motivated elsewhere and don't consume it
                    if c["server_id"] in novel_ids \
                            and not c.get("pinned") \
                            and c["server_id"] != target:
                        novel_picked = True
                    allowance -= c["eff_hr"]
                    budget_left -= _est_bound(c, term)
                # loss_mining: a gated-out challenger is still a
                # free-window audition -- the same "below-floor
                # beats idle" rule as slot fills
                for c in relaxed:
                    if len(picks) >= needed or \
                            len(state["rentals"]) + len(chosen) \
                            + len(picks) >= \
                            cfg["max_rentals"] + cfg["burst_max"]:
                        break
                    why = _admit(c, ungated=True)
                    if why == "over_budget":
                        over_budget.append(c)
                        continue
                    if why:
                        continue
                    c["loss_mining"] = True
                    picks.append(c)
                    allowance -= c["eff_hr"]
                    budget_left -= _est_bound(c, term)
                for pick in picks:
                    chosen.append(pick)
                    rent_actions.append({
                        "type": "rent", "server_id": pick["server_id"],
                        "reason": "probe", "tier": pick["tier"],
                        "role": "probe", "burst_id": burst["burst_id"],
                        "hours": term,
                        "gpu_count": pick.get("gpu_count") or 1,
                        **({"pinned": True} if pick.get("pinned")
                           else {}),
                        **_spot_fields(pick)})
                if say:
                    skipped_pins = [
                        f"{sid}({pin_reasons.get(sid, 'no_candidate')})"
                        for sid in pin_rank
                        if sid not in {
                            p["server_id"] for p in picks}]
                    say(f"{_iso(now)} note burst {burst['burst_id']} "
                        "fill: "
                        + ("loss_mining "
                           if any(p.get("loss_mining") for p in picks)
                           else "")
                        + _pool_summary(
                            probes_now, picks[0] if picks else None,
                            over_budget, rejects, state["remaining"],
                            term, target)
                        + (" | pins skipped " + ",".join(skipped_pins)
                           if skipped_pins else ""))
        # pending rerents due: re-rent the same server while it is
        # still listed, but only through the same gates a fresh fill
        # would face -- its pending reservation must not mask it from
        # the candidate pool, so the pool sees an empty rerent list
        excess_sids = {r["server_id"] for r in state["rentals"]
                       if r["order_id"] in excess_ids}
        for pending in ([] if lull else state["pending_rerents"]):
            due = pending["due_at"]
            if now > due + _minutes(cfg["rerent_grace_min"]):
                actions.append({"type": "rerent_expired",
                                "server_id": pending["server_id"]})
                continue
            if pending["server_id"] in excess_sids:
                # over-target fleet under no_renew: the weakest
                # excess slot's reservation is withdrawn so it
                # exits at term end without a replacement
                actions.append({"type": "rerent_expired",
                                "server_id": pending["server_id"]})
                continue
            if now < due:
                continue
            sid = pending["server_id"]
            row = state["scored"].get(sid)
            cand = next(
                (c for c in _candidates(
                    {**state, "pending_rerents": []},
                    cfg, active_ids, chosen, term=term,
                    spot_mode=cfg["spot_slot_mode"])
                 if c["server_id"] == sid), None)
            if cand is None:
                why = ("excluded" if sid in state["excluded"]
                       else "quote_rejected"
                       if sid in state["quote_rejected"]
                       else "unlisted" if row is None
                       else row["ineligible_reason"]
                       or ("no price"
                           if row["hourly_cost_usd"] is None
                           else "low_rel"
                           if (row.get("reliability") is None
                               or row["reliability"]
                               < cfg["min_reliability"])
                           else f"${row['hourly_cost_usd']}"
                                "/h over cap"))
            elif _below_floor(cand):
                why = "below_floor"
            elif _below_return(cand):
                why = "below_return"
            elif cand["eff_hr"] > headroom_hr and (
                    not _evidenced(cand)
                    or n_slots + n_chosen_slots >= cfg["slot_min"]):
                # below slot_min pace does not bind -- but only for
                # measured candidates; an unmeasured rig still waits
                # for headroom or a bounded probe. The quote budget
                # always caps spend either way
                why = "over_pace"
            elif not _quote_fits(cand, term, state["remaining"]):
                why = "over_budget"
            else:
                why = None
            if why is None:
                rent_actions.append({"type": "rent", "server_id": sid,
                                     "reason": "rerent",
                                     "tier": pending["tier"],
                                     "hours": term,
                                     "gpu_count": cand.get("gpu_count") or 1,
                                     **_spot_fields(cand)})
                chosen.append(cand)
                rerented_ids.add(sid)
                n_chosen_slots += 1
            elif say:
                say(f"{_iso(now)} note rerent s{sid} skipped: {why}")
        # fill vacancies with the highest-value fleet the pace can
        # sustain: an empty slot is fine when nothing fits inside
        # headroom -- the budget is a ceiling, not a spend target.
        # Probes don't consume slots and retry bursts are pure
        # auditions; only due rerents reserve their vacancies.
        vacancies = state["slot_target"] - (
            n_slots + n_chosen_slots
            + sum(1 for p in state["pending_rerents"]
                  if p["server_id"] not in rerented_ids))
        if vacancies > 0 and not lull:
            rejects = {}
            candidates = _candidates(state, cfg, active_ids, chosen,
                                     rejects, term=term,
                                     spot_mode=cfg["spot_slot_mode"])
            priced = []
            for c in candidates:
                if c["prior"] is None and not cfg["loss_mining"]:
                    continue
                if _below_floor(c):
                    rejects["below_floor"] = \
                        rejects.get("below_floor", 0) + 1
                    continue
                if _below_return(c):
                    rejects["below_return"] = \
                        rejects.get("below_return", 0) + 1
                    continue
                if _quote_fits(c, term, state["remaining"]):
                    priced.append(c)
            best_set, best_value = [], 0.0
            for size in range(1, vacancies + 1):
                for combo in itertools.combinations(
                        priced, size):
                    # below slot_min pace does not bind -- but only
                    # for combos of measured candidates; an
                    # unmeasured rig still waits for headroom or a
                    # bounded probe. The remaining-budget bound just
                    # below always caps spend
                    if sum(c["eff_hr"] for c in combo) > headroom_hr \
                            and (n_slots + n_chosen_slots
                                 >= cfg["slot_min"]
                                 or not all(
                                     _evidenced(c) for c in combo)):
                        continue
                    if sum(_est_bound(c, term) for c in combo) \
                            > state["remaining"]:
                        continue
                    value = sum(
                        _candidate_score(c) if usd_hr_fn is not None
                        else (c["value"] or 0.0) for c in combo)
                    if value > best_value:
                        best_value, best_set = value, list(combo)
            if say:
                if best_set:
                    say(f"{_iso(now)} note fill: " + _pool_summary(
                        candidates, best_set[0],
                        [c for c in candidates
                         if not _quote_fits(
                             c, term, state["remaining"])],
                        rejects, state["remaining"], term))
                else:
                    top = next(iter(priced), next(iter(candidates),
                                                  None))
                    label = (_cand_label(top, term)
                             if top is not None
                             else "no eligible candidates")
                    rej = ("rejected " + ",".join(
                        f"{k}={n}" for k, n in sorted(
                            rejects.items(), key=lambda kv: -kv[1]))
                        + " " if rejects else "")
                    say(f"{_iso(now)} note fill: none fits pace "
                        f"headroom ${headroom_hr:.3f}/h (sustainable "
                        f"${pacing['sustainable_hr']:.3f}/h, burn "
                        f"${pacing['slot_burn_hr']:.3f}/h, floor "
                        + ("ret " if usd_hr_fn is not None else "v/$ ")
                        + (f"{value_floor:.1f}" if value_floor is not None
                           else "-")
                        + f"); {rej}best {label}")
            # never-idle fallback: with every slot empty, nothing
            # reserving a vacancy, and budget left, the best-return
            # listing is still worth renting for the largest term
            # that fits -- headroom limits spend rate, not whether an
            # idle slot gets one more tree
            fb_pick = None
            if not best_set and n_slots == 0 \
                    and not state["unjudged_bursts"] \
                    and not state["pending_bursts"] \
                    and not state["pending_rerents"] \
                    and state["remaining"] > 0:
                for h in range(term, 0, -1):
                    # ranking, fee amortization and the return gate
                    # all shift with the term actually bought, so the
                    # pool is repriced for each candidate term
                    fb_cands = _candidates(state, cfg, active_ids,
                                           chosen, term=h,
                                           spot_mode=cfg[
                                               "spot_slot_mode"])
                    fb_best = next(
                        (c["prior"] for c in fb_cands
                         if c["prior"] is not None), None)
                    fb_floor = (fb_best * cfg["min_value_ratio"]
                                if fb_best is not None else None)
                    if usd_hr_fn is not None:
                        fb_cands.sort(key=lambda c: -_candidate_score(c, h))
                    for c in fb_cands:
                        if c["prior"] is None:
                            continue
                        if fb_floor is not None \
                                and c["prior"] < fb_floor:
                            continue
                        if usd_hr_fn is not None:
                            ret = predicted_return(
                                c["usd_hr"], c["hourly"], h,
                                c.get("creation_fee",
                                      _CREATION_FEE_USD))
                            if ret is None \
                                    or ret < cfg["min_return"]:
                                continue
                        if _est_bound(c, h) <= state["remaining"]:
                            fb_pick = (c, h)
                            break
                    if fb_pick is not None:
                        break
                if fb_pick is not None:
                    c, h = fb_pick
                    chosen.append(c)
                    n_chosen_slots += 1
                    rent_actions.append({
                        "type": "rent", "server_id": c["server_id"],
                        "reason": "fill", "tier": c["tier"],
                        "hours": h,
                        "gpu_count": c.get("gpu_count") or 1,
                        **_spot_fields(c)})
                    if say:
                        say(f"{_iso(now)} note fill: idle fallback s"
                            f"{c['server_id']} {h}h (headroom "
                            f"${headroom_hr:.3f}/h)")
            # loss_mining: a vacancy the gated combo left empty is
            # still better filled by a below-floor rig -- the upgrade
            # scan replaces it as soon as a stronger listing frees,
            # and an unmeasured rig pays in data even when its
            # estimate was right. Pace and the quote budget still
            # bind (except a fully-idle fleet, same "one more tree"
            # rule as the idle fallback); the floor and return gate
            # are the only things relaxed. Unmeasured rigs rank
            # first -- the fill is also the measurement, so the
            # least-known listing teaches most.
            if cfg["loss_mining"] and len(best_set) < vacancies \
                    and fb_pick is None:
                cap = headroom_hr if n_slots > 0 else math.inf
                cap -= sum(c["eff_hr"] for c in best_set)
                # the gated picks already committed their quote
                # bound; relaxed picks each admit on what is left
                budget_left = state["remaining"] - sum(
                    _est_bound(c, term) for c in best_set)
                picked = {id(c) for c in best_set}
                relaxed = sorted(
                    (c for c in candidates
                     if id(c) not in picked
                     and c["eff_hr"] <= cap
                     and _quote_fits(c, term, budget_left)),
                    key=lambda c: (
                        0 if c["extrapolated"]
                        else 1 if c["prior"] is None else 2,
                        -_candidate_score(c)))
                for c in relaxed:
                    if len(best_set) >= vacancies:
                        break
                    if c["eff_hr"] > cap:
                        continue
                    c["loss_mining"] = True
                    best_set.append(c)
                    cap -= c["eff_hr"]
                if any(c.get("loss_mining") for c in best_set) and say:
                    say(f"{_iso(now)} note fill: loss_mining "
                        + _pool_summary(
                            candidates,
                            next(c for c in best_set
                                 if c.get("loss_mining")), [],
                            rejects, state["remaining"], term))
            def _emit_key(c):
                # loss_mining picks can lack a prior entirely
                s = (_candidate_score(c) if usd_hr_fn is not None
                     else c["value"])
                return (s if isinstance(s, (int, float))
                        and math.isfinite(s) else 0.0)
            for c in sorted(best_set, key=_emit_key, reverse=True):
                if cfg["loss_mining"] and _gate_violation(c):
                    c["loss_mining"] = True
                chosen.append(c)
                n_chosen_slots += 1
                rent_actions.append({"type": "rent",
                                     "server_id": c["server_id"],
                                     "reason": ("loss_mining"
                                                if c.get("loss_mining")
                                                else "fill"),
                                     "tier": c["tier"],
                                     "hours": term,
                                     "gpu_count": c.get("gpu_count") or 1,
                                     **_spot_fields(c)})
    if blocks:
        actions.extend({"type": "blocked", "reason": reason}
                       for reason in blocks)
    else:
        actions.extend(rent_actions)
    if not cfg["probes_enabled"]:
        # one marker per cycle: the toggle is live even in cycles
        # where no trigger wanted a burst
        actions.append({"type": "blocked", "reason": "probes_off"})
    return actions


_QUOTE_PAD_USD = Decimal("0.20")
_CREATION_FEE_USD = Decimal("0.10")  # flat marketplace order fee
_SPOT_CREATION_FEE_USD = Decimal("0.005")  # spot order creation fee
# balance gating reads the collector's wallet snapshot: a live
# /v1/wallets call per rent attempt is rate-limit bait during
# multi-burst ticks. Snapshots land every ~5 min, so fresh enough to
# spend on is well under a collector cycle miss.
_BALANCE_SNAPSHOT_FRESH_S = 600   # skip the live fetch entirely below this
_BALANCE_SNAPSHOT_MAX_S = 900     # beyond this a failed fetch still blocks


def _est_bound(c, hours):
    """Rough quote bound before the API call: hourly*hours plus a pad
    covering the creation fee and cost margin."""
    if c["hourly"] is None:
        return None
    return _dec(c["hourly"]) * hours + _QUOTE_PAD_USD


def _quote_fits(c, hours, remaining):
    est = _est_bound(c, hours)
    return est is not None and est <= remaining


def _spot_fields(c):
    """Spot identity on a rent action; absent keys keep on-demand
    actions unchanged from before spot support."""
    if c.get("mode") != "spot":
        return {}
    return {"mode": "spot", "hourly": c.get("hourly"),
            "spot_bid_usd_day": c.get("spot_bid_usd_day")}


def _share_price_cap(state, cfg):
    """Raised candidate price cap while pool-share targeting is
    armed and behind: enabled + funded approval + capacity deficit
    lets throughput rigs past max_price_hr (value gates still
    apply). Returns None when the override isn't armed."""
    ps = state.get("pool_share") or {}
    if not ps.get("enabled") \
            or not (ps.get("approved") or {}).get("usd") \
            or (ps.get("deficit_its") or 0) <= 0:
        return None
    cap = _dec(cfg.get("pool_share_max_price_hr")
               or cfg["max_price_hr"])
    return max(cap, _dec(cfg["max_price_hr"]))


def _candidates(state, cfg, active_ids, chosen, rejects=None,
                term=None, spot_min_discount=None, spot_mode="auto",
                price_cap=None):
    """Acceptable listings minus active/pending/excluded, sorted by
    fee-amortized value/$ prior (hourly + creation fee/`term`), then
    hourly, then id. When the pool-snapshot value model is available,
    prior is the predicted return multiple (usd_hr / eff_hr) and value
    the predicted $/h; otherwise value is est_its**its_exponent.
    When a `rejects` dict is given it gets {reason: count} for
    listings that were scored but filtered out -- decision-note
    material.
    With spot_enabled a cached usable spot quote that discounts the
    on-demand hourly by at least `spot_min_discount` switches the
    candidate to spot pricing, bid and creation fee; missing or
    unverifiable quotes leave it on-demand. `spot_mode` "off" keeps
    every candidate on-demand; "only" rejects candidates whose spot
    quote is missing or doesn't clear the discount; "auto" and
    "prefer" quote identically (prefer only changes the fallback on
    a failed recheck)."""
    if term is None:
        term = cfg["hours"]
    if spot_min_discount is None:
        spot_min_discount = cfg["spot_min_discount"]
    if spot_mode == "off":
        spot_min_discount = 1.0
    spot_quotes = state.get("spot_quotes") or {}
    spot_cooling = state.get("spot_outbid_cooldown") or set()
    usd_hr_fn = (state.get("value_model") or {}).get("usd_hr")
    pending_ids = {p["server_id"] for p in state["pending_rerents"]}
    chosen_ids = {c["server_id"] for c in chosen}
    try:
        cpu_blocked = re.compile(
            cfg.get("cpu_blocklist_re") or "", re.IGNORECASE)
    except re.error:
        cpu_blocked = None
    pool = []

    def _reject(reason):
        if rejects is not None:
            rejects[reason] = rejects.get(reason, 0) + 1

    for sid, row in state["scored"].items():
        if sid in active_ids:
            _reject("active")
            continue
        if sid in pending_ids:
            _reject("pending_rerent")
            continue
        if sid in chosen_ids:
            _reject("chosen")
            continue
        if sid in state["excluded"]:
            _reject("excluded")
            continue
        if sid in state["quote_rejected"]:
            _reject("quote_rejected")
            continue
        if row.get("owner") in (state.get("bad_owners") or ()):
            _reject("bad_owner")
            continue
        rel = row.get("reliability")
        if cfg["min_reliability"] > 0 and \
                (rel is None or rel < cfg["min_reliability"]):
            _reject("low_rel")
            continue
        if cpu_blocked is not None and cpu_blocked.pattern \
                and cpu_blocked.search(row["cpu"] or ""):
            _reject("cpu_blocked")
            continue
        if row["ineligible_reason"] not in (None, "no matching benchmark"):
            _reject(row["ineligible_reason"] or "ineligible")
            continue
        hourly = row["hourly_cost_usd"]
        if hourly is None:
            _reject("no_price")
            continue
        cap = (price_cap if price_cap is not None
               else _share_price_cap(state, cfg)
               or _dec(cfg["max_price_hr"]))
        if hourly > cap:
            _reject("over_cap")
            continue
        cpu = row["cpu"]
        gpu = row["gpu"]
        gpu_n = row.get("gpu_count") or 1
        coverage = state["stats"]["coverage"].get((cpu, gpu), 0.0)
        est, extrapolated, cross_ep = est_its(
            state["stats"], sid, cpu, gpu, gpu_count=gpu_n)
        quote = spot_quotes.get(sid)
        spot_hourly = None
        if quote is not None and sid not in spot_cooling:
            spot_hourly = quote["bid_usd_day"] / 24 \
                * (1 + market.RENTER_FEES["spot"])
        od_hourly = float(hourly)
        use_spot = spot_hourly is not None and \
            spot_hourly <= od_hourly * (1 - spot_min_discount)
        if spot_mode == "only" and not use_spot:
            _reject("no_spot")
            continue
        mode = "spot" if use_spot else "on-demand"
        hourly_chosen = spot_hourly if use_spot else od_hourly
        creation_fee = (_SPOT_CREATION_FEE_USD if use_spot
                        else _CREATION_FEE_USD)
        eff_hr = hourly_chosen + float(creation_fee) / term
        usd_hr_est = (usd_hr_fn(est)
                      if usd_hr_fn is not None and est is not None
                      else None)
        if usd_hr_fn is not None:
            value = usd_hr_est
            return_x = predicted_return(usd_hr_est, hourly_chosen,
                                        term, creation_fee)
            prior = return_x
        else:
            value = _value(est, cfg)
            prior = value / eff_hr if value is not None else None
            return_x = None
        pool.append({
            "server_id": sid, "cpu": cpu, "gpu": gpu,
            "gpu_count": gpu_n,
            "hourly": hourly_chosen, "prior": prior,
            "coverage": coverage,
            "eff_hr": eff_hr, "value": value,
            "usd_hr": usd_hr_est, "return_x": return_x,
            "est_its": est, "extrapolated": extrapolated,
            "cross_epoch": cross_ep,
            "mode": mode, "creation_fee": creation_fee,
            "od_hourly": od_hourly, "spot_hourly": spot_hourly,
            "spot_bid_usd_day": (quote["bid_usd_day"]
                                 if use_spot else None),
            "spot_floor_usd_day": (quote["floor_usd_day"]
                                   if use_spot else None),
            "tier": "zen" if ZEN_RE.search(cpu) else "other",
        })
    pool.sort(key=lambda c: (
        c["prior"] is None, -(c["prior"] or 0),
        c["hourly"], c["server_id"]))
    return pool


def _cand_label(c, term, target=None):
    """One candidate on a decision note line: id, tier, price, the
    predicted prior (return multiple when the value model is
    available, v/$ otherwise) and the estimated quote bound."""
    if c.get("return_x") is not None:
        net = forecast_profit(c["usd_hr"], c["hourly"], term,
                              c.get("creation_fee", _CREATION_FEE_USD))
        rating = (f"net=${net:.3f}/h ret={c['return_x']:.2f}x "
                  f"gross=${c['usd_hr']:.3f}/h")
    else:
        prior = (f"{c['prior']:.1f}"
                 if c["prior"] is not None else "-")
        rating = f"v/$={prior}"
    mark = " target" if target == c["server_id"] else ""
    est = _est_bound(c, term)
    return (f"s{c['server_id']}[{c['tier']}]{mark} "
            f"${float(c['hourly']):.3f}/h {rating} "
            f"cov={c['coverage']:.2f}"
            + (f" est${est:.2f}" if est is not None else ""))


def _pool_summary(cands, pick, over_budget, rejects, remaining, term,
                  target=None):
    """One-line pool decision note: who was eligible, who got picked,
    who was priced out ahead of the pick, and the next runners-up."""
    bits = [f"{len(cands)} eligible"]
    if rejects:
        bits.append("rejected " + ",".join(
            f"{k}={n}" for k, n in sorted(
                rejects.items(), key=lambda kv: -kv[1])))
    if pick is not None:
        bits.append("-> pick " + _cand_label(pick, term, target))
        for c in over_budget:
            est = _est_bound(c, term)
            bits.append(
                f"s{c['server_id']} est${est:.2f}>rem"
                if est is not None
                else f"s{c['server_id']} no_price")
        nxt = [c for c in cands if c not in over_budget
               and c["server_id"] != pick["server_id"]][:3]
        if nxt:
            bits.append("next " + ", ".join(
                _cand_label(c, term) for c in nxt))
    elif cands:
        cheapest = min(
            (_est_bound(c, term) for c in cands
             if c["hourly"] is not None), default=None)
        why = (f"cheapest est ${cheapest:.2f}"
               if cheapest is not None else "no priced candidates")
        bits.append(f"none affordable ({why} > remaining "
                    f"${remaining:.2f})")
    return " ".join(bits)


def _active_burn(db, phase_id, now):
    """Remaining USD burn of active rentals: hourly x hours to
    deadline (0 past it)."""
    need = Decimal(0)
    for row in db.execute(
            "SELECT e.hourly_usd, f.deadline_at FROM "
            "experiment_rentals e LEFT JOIN fleet_rentals f "
            "ON f.order_id=e.order_id WHERE e.phase_id=? "
            "AND e.ended_at IS NULL", (phase_id,)):
        deadline = _dt(row["deadline_at"])
        if deadline is None or deadline <= now:
            continue
        hours_left = Decimal(str(
            (deadline - now).total_seconds())) / Decimal(3600)
        need += _dec(row["hourly_usd"]) * hours_left
    return need


def _last_cancel_failed(db, phase_id, order_id):
    row = db.execute(
        "SELECT ts FROM experiment_events WHERE phase_id=? AND "
        "order_id=? AND kind='cancel_failed' ORDER BY id DESC LIMIT 1",
        (phase_id, order_id)).fetchone()
    return _dt(row["ts"]) if row else None


def runner_alive(db_path):
    """True while a `run` process holds the exclusive experiment lock.
    None of the live-db writes happen here: read-only flock probe."""
    lock_path = os.path.expanduser(db_path) + ".experiment.lock"
    try:
        fd = os.open(lock_path, os.O_RDONLY)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True  # LOCK_EX held by the runner
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def _countdown(when, now):
    if when is None:
        return None
    mins = int((when - now).total_seconds() // 60)
    return "now" if mins <= 0 else f"{mins}m"


def next_step(r, now, cfg, probe_for=None):
    """The soonest upcoming action for one rental, as display text --
    mirrors decide()'s per-rental rules without emitting anything."""
    role = r["role"]
    if role == "probe":
        mode = r.get("mode")
        due = [(r["created_at"]
                + _minutes(_probe_cancel_min(mode, cfg)), "deadline")]
        if r.get("t0") is not None:
            due.append((r["t0"] + _minutes(_probe_judge_min(mode, cfg)),
                        "judged"))
        when, label = min(due, key=lambda d: d[0])
        suffix = f" · probe for s{probe_for}" \
            if probe_for is not None else ""
        return f"{label} in {_countdown(when, now)}{suffix}"
    if role in ("kept", "control"):
        when = r.get("deadline_at")
        if when is None:
            return "runs to term end"
        return f"runs to term end in {_countdown(when, now)}"
    t0 = r["t0"]
    if t0 is None:
        return "booting"
    quiet = r["quiet_since"] or t0
    produced = r["last_share_at"] is not None
    if r["reroll_start"] is not None:
        give_due = r["reroll_start"] + _minutes(r["x_min"])
        return f"gives up in {_countdown(give_due, now)}"
    if produced:
        if now - r["last_share_at"] < _minutes(cfg["dry_after_min"]):
            ago = int((now - r["last_share_at"]).total_seconds() // 60)
            return f"mining (last share {ago}m ago)"
        dry_due = quiet + _minutes(r["x_min"])
        return f"burst in {_countdown(dry_due, now)}"
    dry_due = quiet + _minutes(cfg["dry_after_min"])
    return f"replace in {_countdown(dry_due, now)}"


def _profit_plan(candidates, hours, available_usd, max_rentals):
    """Fee-amortized steady-fleet projection, after rental/value gates.

    Maximize additive net USD, not revenue or return per dollar.
    available_usd includes refundable future obligations (pace.money),
    not just the balance left after reserving active rental quotes.
    """
    best = []
    if hours > 0:
        for c in candidates:
            revenue, cost = c.get("usd_hr"), c.get("eff_hr")
            if revenue is None or cost is None:
                continue
            if not all(math.isfinite(v) and v >= 0
                       for v in (revenue, cost)):
                continue
            if revenue > cost:
                best.append(c)
        best.sort(key=lambda c: (
            -(c["usd_hr"] - c["eff_hr"]),
            c["eff_hr"], c["server_id"]))
        best = best[:max_rentals]
    cost = sum(c["eff_hr"] for c in best) * max(0.0, hours)
    gross = sum(c["usd_hr"] for c in best) * max(0.0, hours)
    return {
        "cost_usd": cost,
        "gross_usd": gross,
        "profit_usd": gross - cost,
        "additional_usd": max(0.0, cost - available_usd),
        "servers": [c["server_id"] for c in best],
    }


def summary(db, now, db_path=None):
    """Read-only snapshot of the experiment for the TUI. Returns None
    when the experiment tables don't exist; never writes."""
    if db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table'"
            " AND name='experiment_phases'").fetchone() is None:
        return None
    phase = db.execute(
        "SELECT * FROM experiment_phases WHERE closed_at IS NULL"
        " ORDER BY phase_id DESC LIMIT 1").fetchone()
    if phase is None:
        phase = db.execute(
            "SELECT * FROM experiment_phases ORDER BY phase_id DESC"
            " LIMIT 1").fetchone()
    alive = runner_alive(db_path) if db_path else None
    if phase is None:
        return {"phase": None, "rentals": [], "pending_rerents": [],
                "burst": None, "events": [], "runner_alive": alive}
    cfg = validate_config(json.loads(phase["config_json"]))
    state = build_state(db, phase, now, cfg, apply_endings=False,
                        say=lambda *_a, **_k: None)
    events = [{
        "id": e["id"], "ts": e["ts"], "kind": e["kind"],
        "order_id": e["order_id"], "server_id": e["server_id"],
        "detail": json.loads(e["detail_json"])}
        for e in db.execute(
            "SELECT * FROM experiment_events WHERE phase_id=?"
            " ORDER BY id DESC LIMIT 200", (phase["phase_id"],))]
    server_docs = {}
    if db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table'"
            " AND name='servers'").fetchone():
        for row in db.execute(
                "SELECT server_id, latest_json FROM servers"):
            try:
                server_docs[row["server_id"]] = json.loads(
                    row["latest_json"] or "{}")
            except ValueError:
                server_docs[row["server_id"]] = {}
    vm = state.get("value_model") or {}
    usd_hr_fn = vm.get("usd_hr")
    # probe rows name the incumbent server their burst is auditioning
    # for: burst_id -> trigger order's server_id
    order_server = {r["order_id"]: r["server_id"]
                    for r in state["rentals"]}
    probe_for = {}
    for ub in state["unjudged_bursts"]:
        trigger = ub["trigger_order_id"]
        if trigger in order_server:
            probe_for[ub["burst_id"]] = order_server[trigger]
    rentals = []
    for r in state["rentals"]:
        doc = server_docs.get(r["server_id"]) or {}
        tree = trees.current_tree(db, r["order_id"])
        forecast_return = None
        forecast_net = None
        net_source = None
        revenue = None
        shares_h = None
        if r["t0"] is not None:
            # post-warmup producing rate, same window as realized
            # revenue below
            hours_r = (now - r["t0"] - _minutes(2)
                       ).total_seconds() / 3600
            if hours_r > 0:
                shares_h = sum(
                    c for ts, c in r["share_arrivals"]
                    if ts >= r["t0"] + _minutes(2)) / hours_r
        if usd_hr_fn is not None and r["role"] != "weight":
            revenue = None
            if r["role"] == "slot" and r["age"] >= _minutes(
                    cfg["actual_yield_min"]):
                # past the grace window the score is measured yield:
                # post-warmup arrivals priced at the pool's current
                # counted value -- actual, not theoretical
                price, w_bar = vm.get("P"), vm.get("w_bar")
                if price and w_bar and r["t0"] is not None:
                    hours_r = (now - r["t0"] - _minutes(2)
                               ).total_seconds() / 3600
                    if hours_r > 0:
                        revenue = (
                            sum(c for ts, c in r["share_arrivals"]
                                if ts >= r["t0"] + _minutes(2))
                            / hours_r) * w_bar * price
                        net_source = "realized"
            if revenue is None and r["its_total"]:
                revenue = usd_hr_fn(r["its_total"])
            if r["role"] == "probe":
                bound = min(t for t in (
                    r["deadline_at"], state["phase_ends_at"],
                    state["epoch_ends_at"]) if t is not None)
                hours_left = max(0.0, (bound - now).total_seconds() / 3600)
                forecast_return = predicted_return(
                    revenue, r["hourly_usd"], hours_left,
                    _dec(r["creation_fee_usd"]))
                forecast_net = forecast_profit(
                    revenue, r["hourly_usd"], hours_left,
                    _dec(r["creation_fee_usd"]))
            elif revenue is not None and float(r["hourly_usd"]) > 0:
                # Match the upgrade judge: incumbent fee is sunk,
                # whereas retaining a probe incurs a fresh fee.
                forecast_return = revenue / float(r["hourly_usd"])
                forecast_net = forecast_profit(revenue, r["hourly_usd"])
        rentals.append({
            "order_id": r["order_id"], "server_id": r["server_id"],
            "role": r["role"], "tier": r["tier"],
            "mode": r.get("mode"),
            "cpu": (doc.get("specs") or {}).get("cpu"),
            "gpu": market.offered_gpu(doc),
            "hourly_usd": str(r["hourly_usd"]),
            "its_total": r["its_total"],
            "forecast_return_x": forecast_return,
            "forecast_usd_hr": revenue,
            "forecast_net_hr": forecast_net,
            "net_source": net_source,
            "age_s": r["age"].total_seconds(),
            "t0": _iso(r["t0"]),
            "last_share_at": _iso(r["last_share_at"]),
            "shares": sum(n for _, n in r["share_arrivals"]),
            "shares_per_h": round(shares_h, 1)
            if shares_h is not None else None,
            "tree_id": (tree or {}).get("tree_id"),
            # sitting on a fleet-dead tree within the wait bound: the
            # rig holds for the rotation's free redraw instead of a
            # cancel+re-rent (the UI shows this as a pause)
            "tree_wait": bool(
                (tree or {}).get("tree_id") in state["dead_trees"]
                and r.get("tree_since") is not None
                and (now - r["tree_since"])
                <= _minutes(1.5 * (state.get("tree_lifetime_min")
                                   or 20))),
            "x_min": r["x_min"], "y_min": r["y_min"],
            "next": next_step(r, now, cfg,
                             probe_for=probe_for.get(
                                 r["burst_id"]))})
    bursts_out = []
    for ub in state["unjudged_bursts"]:
        ub_probes = state["burst_probes"].get(ub["burst_id"], [])
        probes = [{
            "order_id": p["order_id"], "server_id": p["server_id"],
            "role": p["role"],
            "age_s": p["age"].total_seconds(),
            "shares": sum(n for _, n in p.get("share_arrivals", []))}
            for p in ub_probes]
        judge_in = 0.0
        for p in ub_probes:
            if p["ended"]:
                continue
            mode = p.get("mode")
            waits = [(_minutes(_probe_cancel_min(mode, cfg))
                      - p["age"]).total_seconds()]
            if p.get("t0") is not None:
                waits.append(
                    (p["t0"] + _minutes(_probe_judge_min(mode, cfg))
                     - now).total_seconds())
            judge_in = max(judge_in, min(waits))
        bursts_out.append(
            {"burst_id": ub["burst_id"], "trigger": ub["trigger"],
             "started_at": _iso(ub["started_at"]),
             "n_target": ub["n_target"], "probes": probes,
             "judge_in_s": max(0.0, judge_in)})
    pacing = pace(state, now, cfg)
    hours_to_epoch = pacing["hours_to_epoch"]
    # Project the highest net-profit gated fleet without today's
    # budget/headroom restriction. Fees are amortized over future terms:
    # this is guidance, not an exact renewal/availability schedule.
    epoch_plan = None
    epoch_plan_gross = None
    epoch_plan_profit = None
    epoch_plan_additional = None
    epoch_plan_servers = []
    epoch_plan_reason = "value_unavailable"
    market_fresh = (state["snapshot_age_s"] is not None
                    and 0 <= state["snapshot_age_s"]
                    <= cfg["stale_market_min"] * 60)
    if usd_hr_fn is not None and not market_fresh:
        epoch_plan_reason = "stale_market"
    if hours_to_epoch > 0 and usd_hr_fn is not None and market_fresh:
        term = max(1, min(cfg["hours"], int(hours_to_epoch)))
        plan_pool = _candidates(
            state, cfg,
            {r["server_id"] for r in state["rentals"]}, [], term=term,
            spot_mode=cfg["spot_slot_mode"])
        for r in state["rentals"]:
            if r["role"] == "slot" and r["its_total"] \
                    and float(r["hourly_usd"]) > 0:
                hourly = float(r["hourly_usd"])
                # a future plan term re-pays the creation fee, so the
                # incumbent is priced like a fresh candidate
                eff_hr = hourly + float(r["creation_fee_usd"]) / term
                value = usd_hr_fn(r["its_total"])
                plan_pool.append({
                    "server_id": r["server_id"], "hourly": hourly,
                    "eff_hr": eff_hr, "value": value,
                    "usd_hr": value,
                    "creation_fee": r["creation_fee_usd"],
                    "prior": (value / eff_hr
                              if value is not None else None)})
        plan_pool.sort(
            key=lambda c: (c["prior"] is None, -(c["prior"] or 0.0),
                           c["hourly"], c["server_id"]))
        # EP only plans rigs it would actually rent: known prior,
        # above the relative floor, and -- under the value model --
        # clearing min_return on the fee-inclusive term
        plan_best_prior = next(
            (c["prior"] for c in plan_pool
             if c["prior"] is not None), None)
        plan_floor = (plan_best_prior * cfg["min_value_ratio"]
                      if plan_best_prior is not None else None)
        eligible = []
        for c in plan_pool:
            if c["prior"] is None or c["value"] is None:
                continue
            if plan_floor is not None and c["prior"] < plan_floor:
                continue
            if usd_hr_fn is not None:
                ret = predicted_return(
                    c["usd_hr"], c["hourly"], term,
                    c.get("creation_fee", _CREATION_FEE_USD))
                if ret is None or ret < cfg["min_return"]:
                    continue
            eligible.append(c)
        plan = _profit_plan(
            eligible, hours_to_epoch, pacing["money"], cfg["max_rentals"])
        epoch_plan = plan["cost_usd"]
        epoch_plan_gross = plan["gross_usd"]
        epoch_plan_profit = plan["profit_usd"]
        epoch_plan_additional = plan["additional_usd"]
        epoch_plan_servers = plan["servers"]
        epoch_plan_reason = None
    # the runner's current market pool, scored exactly as decide()
    # does, so the TUI can show the same forecasts instead of
    # recomputing them. fill_status annotates only candidate-level
    # checks -- global blocks and reservations can still differ.
    # A closed phase exports no pool so the TUI falls back to its
    # static eligibility view.
    hours_to_phase = max(
        0.0, (state["phase_ends_at"] - now).total_seconds() / 3600)
    rent_window_h = min(hours_to_epoch, hours_to_phase)
    actual_term = max(1, min(cfg["hours"], int(rent_window_h)))
    candidate_values = None
    if phase["closed_at"] is None:
        cand_rows = _candidates(
            state, cfg,
            {r["server_id"] for r in state["rentals"]}, [],
            term=actual_term, spot_mode=cfg["spot_slot_mode"])
        cand_best_prior = next(
            (c["prior"] for c in cand_rows
             if c["prior"] is not None), None)
        cand_floor = (cand_best_prior * cfg["min_value_ratio"]
                      if cand_best_prior is not None else None)
        candidate_values = []
        for c in cand_rows:
            if c["prior"] is None:
                status = "unknown value"
            elif cand_floor is not None and c["prior"] < cand_floor:
                status = "below floor"
            elif usd_hr_fn is not None and (
                    c["return_x"] is None
                    or c["return_x"] < cfg["min_return"]):
                status = "below return"
            elif not _quote_fits(c, actual_term, state["remaining"]):
                status = "over budget"
            elif c["eff_hr"] > pacing["headroom_hr"]:
                status = "fill pace"
            elif rent_window_h < 1:
                status = "term"
            else:
                status = "value ok"
            sp = (state["stats"].get("server_prior") or {}).get(
                c["server_id"]) or {}
            th_its, th_x, th_c = (
                est_its(state["stats"], c["server_id"], c["cpu"],
                        c["gpu"], gpu_count=c.get("gpu_count") or 1,
                        use_server=False)
                if c.get("cpu") and c.get("gpu")
                else (None, False, False))
            candidate_values.append({
                "server_id": c["server_id"],
                "gpu": c.get("gpu"),
                "cpu": c.get("cpu"),
                "meas_its": sp.get("its"),
                "meas_rentals": sp.get("rentals") or 0,
                "meas_cross_epoch": bool(sp.get("cross_epoch")),
                "est_its": th_its,
                "extrapolated": th_x,
                "cross_epoch": th_c,
                "usd_hr": c["usd_hr"],
                "return_x": c["return_x"],
                "net_hr": forecast_profit(
                    c["usd_hr"], c["hourly"], actual_term,
                    c.get("creation_fee", _CREATION_FEE_USD)),
                "prior": c["prior"],
                "eff_hr": c["eff_hr"],
                "hourly": float(c["hourly"]),
                "mode": c.get("mode"),
                "od_hourly": c.get("od_hourly"),
                "spot_hourly": c.get("spot_hourly"),
                "spot_discount": (
                    1 - c["spot_hourly"] / c["od_hourly"]
                    if c.get("spot_hourly") and c.get("od_hourly")
                    else None),
                "fill_status": status,
                # upgrade-audition eligibility: actively cooling after
                # a non-promoting judgement, cleared for a bounded
                # retry, spacing out before one, phase-locked, or free
                "upgrade_status": (
                    ("cd " + str(max(0, math.ceil(
                        (until - now).total_seconds() / 60))) + "m")
                    if (until := state.get(
                        "upgrade_cooldown_until", {}).get(
                        c["server_id"])) is not None and until > now
                    else "retryable"
                    if c["server_id"] in state.get(
                        "upgrade_retryable", set())
                    else ("cd " + str(max(0, math.ceil(
                        (pending - now).total_seconds() / 60))) + "m")
                    if c["server_id"] in state["tried_servers"]
                    and (pending := state.get(
                        "upgrade_pending_until", {}).get(
                        c["server_id"])) is not None
                    else "tried"
                    if c["server_id"] in state["tried_servers"]
                    else "ok")})
    return {
        "phase": {
            "phase_id": phase["phase_id"],
            "started_at": phase["started_at"],
            "ends_at": phase["ends_at"],
            "budget": f"{_dec(phase['budget_usd']):.2f}",
            "reserve": f"{_dec(phase['reserve_usd']):.2f}",
            "committed": f"{state['committed']:.2f}",
            "remaining": f"{state['remaining']:.2f}",
            "active_burn_hr": f"{float(state['active_burn_hr']):.3f}",
            "optimal_burn_hr": f"{pacing['sustainable_hr']:.3f}",
            "headroom_hr": f"{pacing['headroom_hr']:.3f}",
            "epoch_plan_usd": (f"{epoch_plan:.2f}"
                               if epoch_plan is not None else None),
            "epoch_plan_gross_usd": (
                f"{epoch_plan_gross:.2f}"
                if epoch_plan_gross is not None else None),
            "epoch_plan_profit_usd": (
                f"{epoch_plan_profit:.2f}"
                if epoch_plan_profit is not None else None),
            "epoch_plan_additional_usd": (
                f"{epoch_plan_additional:.2f}"
                if epoch_plan_additional is not None else None),
            "epoch_plan_servers": epoch_plan_servers,
            "epoch_plan_method": "steady_fleet_profit",
            "epoch_plan_reason": epoch_plan_reason,
            "epoch_plan_phase_limited": (
                state["phase_ends_at"] < state["epoch_ends_at"]),
            "value_model": (
                {k: vm[k] for k in
                 ("source", "P", "w_bar", "S", "beta", "k", "its_ref",
                  "confidence")}
                if vm.get("source") not in (None, "unavailable")
                else {"source": "unavailable"}),
            # the static eligibility gates _candidates applies, so the
            # TUI can mark marketplace rows the same way -- plus the
            # score yardstick and the gates the strategy bundles move
            "min_reliability": cfg["min_reliability"],
            "max_price_hr": float(_dec(cfg["max_price_hr"])),
            "loss_mining": cfg["loss_mining"],
            "score": cfg["score"],
            "upgrade_margin": cfg["upgrade_margin"],
            "promote_margin": cfg["promote_margin"],
            "min_value_ratio": cfg["min_value_ratio"],
            "min_return": cfg["min_return"],
            "slot_target": state["slot_target"],
            "slot_mode": cfg["slot_mode"],
            "slot_min": cfg["slot_min"],
            "growth_margin": cfg["growth_margin"],
            "shrink_mode": cfg["shrink_mode"],
            "max_rentals": cfg["max_rentals"],
            "paused": cfg["paused"],
            "epoch_ends_at": _iso(state["epoch_ends_at"])},
        "rentals": rentals,
        "pending_rerents": [{
            "server_id": p["server_id"],
            "due_at": _iso(p["due_at"])}
            for p in state["pending_rerents"]],
        "pending_bursts": [{
            "for_order": p["for_order"],
            "due_at": _iso(p["due_at"])}
            for p in state["pending_bursts"]],
        "burst": bursts_out[0] if bursts_out else None,
        "bursts": bursts_out,
        "candidate_values": candidate_values,
        "candidate_term_hours": actual_term,
        "events": events,
        # scout/offering context for the dead-tree pause indicator:
        # agent = freshest scout report (<15min), offering_dead when
        # the pool's current draw is a proven-dead tree
        "agent": state.get("agent"),
        "dead_trees": sorted(state["dead_trees"]),
        "tree_lifetime_min": state.get("tree_lifetime_min"),
        "offering_dead": bool(
            (state.get("agent") or {}).get("tree_id")
            in state["dead_trees"]),
        "pool_share": state.get("pool_share"),
        "runner_alive": alive}


def execute(db, db_path, env_file, phase, cfg, actions, now, live,
            say=print):
    """Run the decided actions. Dry-run only records `dryrun` events
    and never touches SSH, the wallet API, quote, create or cancel."""
    phase_id = phase["phase_id"]
    rented = False
    for action in actions:
        kind = action["type"]
        if kind == "lull":
            _emit_throttled(
                db, now, phase_id, None, None, "lull",
                {"streak": action["streak"],
                 "until": action["until"],
                 "note": action["note"]},
                "until", action["until"], say=say)
            continue
        if kind == "offering_dead":
            _emit_throttled(
                db, now, phase_id, None, None, "offering_dead",
                {"tree_id": action["tree_id"],
                 "source": action["source"],
                 "note": action["note"]},
                "tree_id", action["tree_id"], say=say)
            continue
        if kind == "tree_wait":
            _emit_throttled(
                db, now, phase_id, action["order_id"],
                action["server_id"], "tree_wait",
                {"tree_id": action["tree_id"],
                 "note": action["note"]},
                "tree_id", action["tree_id"], say=say)
            continue
        if not live:
            if kind == "blocked":
                _emit_blocked(db, now, phase_id, None, None,
                              action["reason"], say=say)
            elif kind in ("rerent_expired", "retry_expired",
                          "retry_cleared"):
                emit(db, now, phase_id, action.get("for_order"),
                     action.get("server_id"), kind,
                     {"server_id": action.get("server_id"),
                      "for_order": action.get("for_order")}, say=say)
            else:
                sig_key, sig_val = ("reason", action["reason"]) \
                    if action.get("reason") \
                    else ("type", action["type"])
                _emit_throttled(
                    db, now, phase_id, action.get("order_id"),
                    action.get("server_id"), "dryrun", action,
                    sig_key, sig_val, say=say)
            continue
        if kind == "blocked":
            _emit_blocked(db, now, phase_id, None, None,
                          action["reason"], say=say)
        elif kind == "blacklist":
            _exec_blacklist(db, phase_id, action, now, say)
        elif kind in ("rerent_expired", "retry_expired",
                      "retry_cleared"):
            emit(db, now, phase_id, action.get("for_order"),
                 action.get("server_id"), kind,
                 {"server_id": action.get("server_id"),
                  "for_order": action.get("for_order")}, say=say)
        elif kind == "burst":
            _exec_burst(db, phase_id, action, now, say)
        elif kind == "judge":
            _exec_judge(db, db_path, env_file, phase_id, action, now,
                        say, free_window_min=cfg["free_window_min"])
        elif kind == "cancel":
            _exec_cancel(db, db_path, env_file, phase_id, action, now,
                         say,
                         free_window_min=cfg["free_window_min"])
        elif kind == "rent":
            # slot fills stay one-per-cycle; burst probes all go in
            # the same cycle so their audition windows start together
            if action.get("role") != "probe" and rented:
                _emit_blocked(db, now, phase_id, None,
                              action["server_id"], "one_rent_per_cycle",
                              say=say)
                continue
            _exec_rent(db, db_path, env_file, phase, cfg, action, now,
                       say)
            if action.get("role") != "probe":
                rented = True


def _exec_burst(db, phase_id, action, now, say):
    """Record the burst; renting probes happens via decide's fill."""
    with db:
        cur = db.execute(
            "INSERT INTO experiment_bursts(phase_id, started_at,"
            " trigger, trigger_order_id, n_target)"
            " VALUES (?,?,?,?,?)",
            (phase_id, _iso(now), action["trigger"],
             action.get("trigger_order_id"), action["n"]))
    emit(db, now, phase_id, action.get("trigger_order_id"), None,
         "burst_started",
         {"burst_id": cur.lastrowid, "trigger": action["trigger"],
          "n": action["n"], "y_min": action.get("y_min"),
          "server_id": action.get("server_id")}, say=say)


def _exec_judge(db, db_path, env_file, phase_id, action, now, say,
                free_window_min=None):
    # kept probes keep their orders -- and their proven trees
    for oid in [action.get("promote")] + [
            e["order_id"] for e in action.get("promote_extra") or []]:
        if oid is None:
            continue
        db.execute(
            "UPDATE experiment_rentals SET role='slot' "
            "WHERE order_id=? AND role='probe'",
            (oid,))
    # a succession verdict renews by scheduling a re-rent of the same
    # server for when the incumbent's term ends (auto-renew stays off
    # as a strategy -- the rerent gates at that moment refuse it if a
    # reprice stopped clearing the return gate)
    renew = action.get("renew")
    if renew is not None:
        if renew.get("due_at"):
            emit(db, now, phase_id, renew["order_id"],
                 renew["server_id"], "rerent_scheduled",
                 {"server_id": renew["server_id"],
                  "tier": renew.get("tier"),
                  "due_at": renew["due_at"]}, say=say)
        emit(db, now, phase_id, renew["order_id"],
             renew.get("server_id"), "renewed",
             {"order_id": renew["order_id"]}, say=say)
    db.execute(
        "UPDATE experiment_bursts SET judged_at=? WHERE burst_id=?",
        (_iso(now), action["burst_id"]))
    db.commit()

    def _cancel_ctx(c):
        return {"order_id": c["order_id"],
                "server_id": c["server_id"],
                "tier": c.get("tier"), "reason": c["reason"],
                "its": c.get("its"), "shares": c.get("shares"),
                "age_min": c.get("age_min"), "rerent_at": None}

    for c in action["cancel"]:
        _exec_cancel(db, db_path, env_file, phase_id, _cancel_ctx(c),
                     now, say, free_window_min=free_window_min)
    orig = action.get("cancel_original")
    if orig is not None:
        _exec_cancel(db, db_path, env_file, phase_id,
                     _cancel_ctx(orig), now, say,
                     free_window_min=free_window_min)
    if action.get("retry"):
        retry = action["retry"]
        emit(db, now, phase_id, retry["for_order"],
             retry.get("server_id"), "burst_scheduled", retry, say=say)
    emit(db, now, phase_id, None, None, "burst_judged",
         {"burst_id": action["burst_id"], "verdict": action["verdict"],
          "finders": action["finders"], "promote": action.get("promote"),
          "promoted_extra": action.get("promote_extra") or [],
          "cancelled": [c["order_id"] for c in action["cancel"]],
          "cancel_original": (orig or {}).get("reason"),
          "retry_due_at": (action.get("retry") or {}).get("due_at"),
          "stats": action.get("stats"),
          "renew": action.get("renew"),
          # probes the judge classified as healthy no-share draws;
          # absent on older events, which stay non-retryable
          "tree_retry_order_ids": (
              action.get("tree_retry_order_ids") or [])},
         say=say)


def _exec_cancel(db, db_path, env_file, phase_id, action, now, say,
                 free_window_min=None):
    order_id = action["order_id"]
    failed_at = _last_cancel_failed(db, phase_id, order_id)
    if failed_at is not None \
            and now - failed_at < _minutes(CANCEL_RETRY_MIN):
        # the retry backoff outlives the free-cancel window: inside it,
        # retry every cycle rather than paying the creation fee twice
        row = db.execute(
            "SELECT created_at FROM experiment_rentals WHERE "
            "order_id=?", (order_id,)).fetchone()
        in_free = free_window_min is not None and row is not None \
            and now - _dt(row["created_at"]) < _minutes(
                free_window_min)
        if not in_free:
            _emit_blocked(db, now, phase_id, order_id,
                          action["server_id"], "cancel_retry_backoff",
                          say=say)
            return
    prior = db.execute(
        "SELECT provision_state, created_at FROM fleet_rentals "
        "WHERE order_id=?", (order_id,)).fetchone()
    prior_state = prior["provision_state"] if prior else None
    rc, info = rent.cancel_managed_order(order_id, db_path, env_file,
                                         say=say, warn=say)
    if rc == 0:
        reason = action["reason"]
        blame = reason.startswith("fault:") or reason == "unreachable"
        if prior_state == "missing":
            # Clore ended the order first (e.g. account balance
            # spent): the rig's silence is not evidence of a bad
            # server, and the server stays re-rentable.
            reason, blame = "missing", False
        elif prior_state == "cancelled":
            blame = False
        elif reason == "unreachable":
            inv = orders.read_inventory(db)
            live_ids = orders.fresh_order_ids(inv, now=now)
            paused_ids = orders.paused_order_ids(inv, now=now)
            if paused_ids is not None and order_id in paused_ids:
                # a paused order isn't a dead rig -- it just isn't
                # ours right now. Cancelling it is a bid withdrawal,
                # not evidence against the server.
                reason, blame = "paused", False
            elif live_ids is not None and order_id not in live_ids \
                    and orders.snapshot_postdates(
                        inv, prior["created_at"]):
                fleet_store.set_state(db, order_id, "missing")
                reason, blame = "missing", False
            elif live_ids is None or order_id not in live_ids:
                # Without a fresh inventory postdating the order's
                # creation, a dead host and an ended order are
                # indistinguishable; don't blacklist.
                blame = False
        with db:
            db.execute(
                "UPDATE experiment_rentals SET ended_at=?, end_reason=? "
                "WHERE order_id=?",
                (_iso(now), reason, order_id))
        detail = {"reason": reason, "its": action.get("its"),
                  "shares": action.get("shares"),
                  "age_min": action.get("age_min")}
        if action.get("unreach"):
            detail["unreach"] = action["unreach"]
        if reason != action["reason"]:
            detail["decided"] = action["reason"]
        emit(db, now, phase_id, order_id, action["server_id"], "cancel",
             detail, say=say)
        if blame:
            _exec_blacklist(db, phase_id, {
                **action, "detail": {"order_id": order_id,
                                    "cancel_confirmed": True,
                                    **(action.get("unreach") or {})}},
                now, say)
        if action.get("rerent_at"):
            emit(db, now, phase_id, order_id, action["server_id"],
                 "rerent_scheduled",
                 {"server_id": action["server_id"],
                  "tier": action.get("tier"),
                  "due_at": action["rerent_at"]}, say=say)
        if action.get("retry_burst"):
            emit(db, now, phase_id, order_id, action["server_id"],
                 "burst_scheduled", action["retry_burst"], say=say)
    else:
        emit(db, now, phase_id, order_id, action["server_id"],
             "cancel_failed", {"info": _safe_info(info)}, say=say)


def _safe_info(info):
    return {k: info.get(k) for k in ("order_id", "stage", "state",
                                     "provision_state")}


def _exec_rent(db, db_path, env_file, phase, cfg, action, now, say):
    phase_id = phase["phase_id"]
    sid = action["server_id"]
    if sid in blacklist.active(db, now):
        _emit_blocked(db, now, phase_id, None, sid, "blacklisted",
                      say=say)
        return
    committed = committed_usd(db, phase_id, now, cfg)
    remaining = _dec(phase["budget_usd"]) - committed
    if remaining <= 0:
        _emit_blocked(db, now, phase_id, None, sid, "budget", say=say)
        return
    snap = orders.latest_wallets(db)
    snap_age = ((now - _dt(snap["captured_at"])).total_seconds()
                if snap["captured_at"] else None)
    if snap_age is not None and snap_age < _BALANCE_SNAPSHOT_FRESH_S:
        wallets = snap["wallets"]
    else:
        try:
            api_key, _ = rent.load_secrets(env_file, need_creds=False)
            wallets = orders.sanitize_wallets(
                orders.fetch_wallets(api_key))
        except Exception as exc:
            if snap["wallets"] and snap_age is not None \
                    and snap_age < _BALANCE_SNAPSHOT_MAX_S:
                # Clore rate-limited or unreachable: the last-known
                # balance plus the burn/need check below is a safer
                # gate than refusing every attempt during an outage
                wallets = snap["wallets"]
            else:
                _emit_blocked(db, now, phase_id, None, sid,
                              f"balance: {type(exc).__name__}", say=say)
                return
    balance = next((_dec(w["balance"]) for w in wallets
                    if w["name"] == "USD-Blockchain"), None)
    estimate = None
    hours = action.get("hours", cfg["hours"])
    row = market.score_listing((latest_listings(db)[0] or {}).get(sid)
                               or {})
    est_hourly = action.get("hourly") or row["hourly_cost_usd"]
    if est_hourly:
        estimate = _dec(est_hourly) * hours
    need = _active_burn(db, phase_id, now) + _dec(phase["reserve_usd"]) \
        + (estimate or Decimal(0))
    if balance is None or balance < need:
        _emit_blocked(db, now, phase_id, None, sid, "balance",
                      {"balance": str(balance), "need": str(need)},
                      say=say)
        return
    # decide() ranked on cached quotes, which only cover the
    # scanner's top-N; the authoritative check is a fresh spot-market
    # read on every spot-enabled attempt. When the fresh quote misses
    # the threshold this attempt just goes on-demand rather than
    # failing outright.
    mode = "on-demand"
    spot_bid = spot_floor = spot_discount = None
    spot_fallback = spot_discovered = False
    reuse = None
    if cfg["reuse_aliases"]:
        try:
            # a provably-dead worker identity from an ended rental:
            # the pin doesn't survive (measured 0/3), so this keeps
            # the pool dashboard's worker list bounded rather than
            # chasing the donor's tree. A failed lookup is worth
            # nothing special -- the rent proceeds on a fresh alias.
            reuse = alias_donor(db, now, cfg)
        except Exception:
            reuse = None
    spot_role_mode = (cfg["spot_probe_mode"]
                      if action.get("role") == "probe"
                      else cfg["spot_slot_mode"])
    if cfg["spot_enabled"] and spot_role_mode != "off":
        threshold = (cfg["spot_probe_min_discount"]
                     if action.get("role") == "probe"
                     else cfg["spot_min_discount"])
        cooled = db.execute(
            "SELECT 1 FROM experiment_rentals WHERE phase_id=?"
            " AND server_id=? AND end_reason='outbid'"
            " AND ended_at>=? LIMIT 1",
            (phase_id, sid,
             _iso(now - _minutes(cfg["spot_outbid_min"])))).fetchone()
        fresh = None
        if cooled is None:
            try:
                # a burst can fire several rents in one cycle; keep
                # each fetch behind the scanner's ~1 req/s gap
                time.sleep(spot.REQUEST_GAP_SECONDS)
                fresh = spot.bid_for(
                    {"captured_at": _iso(now),
                     "raw_json": json.dumps(
                         spot.fetch_quote(api_key, sid)),
                     "error_kind": None}, now,
                    premium=cfg["spot_bid_premium"])
            except Exception:
                fresh = None
        od_hourly = (float(row["hourly_cost_usd"])
                     if row["hourly_cost_usd"] else None)
        if fresh is not None and od_hourly:
            spot_hourly = fresh["bid_usd_day"] / 24 \
                * (1 + market.RENTER_FEES["spot"])
            if spot_hourly <= od_hourly * (1 - threshold):
                mode = "spot"
                spot_bid = fresh["bid_usd_day"]
                spot_floor = fresh["floor_usd_day"]
                spot_discount = 1 - spot_hourly / od_hourly
        # fallback is a decide-picked spot that failed the fresh
        # check; a discount first seen here on an on-demand pick is a
        # discovery, not a fallback
        spot_fallback = action.get("mode") == "spot" and mode != "spot"
        spot_discovered = action.get("mode") != "spot" \
            and mode == "spot"
    def _quote(mode2, bid=None, floor=None):
        return rent.quote_order(
            server_id=sid, hours=hours,
            max_cost_usd=str(remaining.quantize(Decimal("0.01"))),
            pool=POOL, cpu_threads=None,
            cpu_threads_cap=cfg["cpu_threads_cap"], db_path=db_path,
            env_file=env_file, key_path=rent.DEFAULT_KEY,
            auto_renew=False, mode=mode2, spot_bid_usd_day=bid,
            spot_floor_usd_day=floor,
            expected_gpu_count=action.get("gpu_count") or 1,
            alias=(reuse or {}).get("alias"), say=say, warn=say)

    def _book(ctx):
        bound = _dec(ctx["quote"]["cost_bound_usd"])
        if bound + committed > _dec(phase["budget_usd"]):
            _emit_blocked(db, now, phase_id, None, sid, "budget",
                          {"bound": str(bound)}, say=say)
            return None, None
        return ctx, rent.create_order_flow(
            ctx, say=say, warn=say,
            defer_provision=action.get("role") == "probe")

    if action.get("pinned"):
        # one pin = one attempt: consumed the moment the order
        # pipeline is entered, success or failure alike -- a repin
        # is the retry. Pre-dispatch blocks (blacklist/budget/
        # balance) never reached this point and leave the pin.
        with db:
            db.execute(
                "DELETE FROM probe_pins WHERE phase_id=? "
                "AND server_id=?", (phase_id, sid))
    try:
        ctx = _quote(mode, spot_bid, spot_floor)
    except Exception as exc:
        detail = {"stage": "quote", "error": type(exc).__name__}
        if isinstance(exc, ValueError):
            # validation/policy messages are safe by design
            detail["detail"] = str(exc)[:200]
        emit(db, now, phase_id, None, sid, "rent_failed",
             detail, say=say)
        return
    ctx, (rc, info) = _book(ctx)
    # spot fell through before the POST: a dead verify fetch is no
    # market verdict and always retries on-demand; a market move
    # (floor or top-bid past our bid at the recheck) retries only
    # under "prefer", where renting this server on-demand beats
    # losing the pick to a bid tick
    retry_od = ctx is not None and mode == "spot" and rc != 0 \
        and info.get("stage") == "spot_recheck" and (
            info.get("spot_fetch_failed") or spot_role_mode == "prefer")
    if retry_od:
        emit(db, now, phase_id, None, sid, "note",
             {"text": ("spot verify fetch failed at POST"
                       if info.get("spot_fetch_failed")
                       else "spot market moved at POST")
              + "; retrying on-demand"}, say=say)
        mode, spot_bid, spot_floor = "on-demand", None, None
        spot_fallback = True
        try:
            ctx, (rc, info) = _book(_quote(mode))
        except Exception as exc:
            emit(db, now, phase_id, None, sid, "rent_failed",
                 {"stage": "quote", "error": type(exc).__name__},
                 say=say)
            return
    if ctx is None:
        return
    if rc != 0 or not info.get("order_id"):
        emit(db, now, phase_id, info.get("order_id"), sid,
             "rent_failed", {"info": _safe_info(info)}, say=say)
        if rc != 0 and info.get("order_id") and \
                info.get("stage") == "provision":
            _exec_blacklist(db, phase_id, {
                "server_id": sid, "order_id": info["order_id"],
                "reason": "provision_failed", "detail": _safe_info(info)},
                now, say)
        return
    order_id = info["order_id"]
    if info.get("provision_state") == "deferred":
        # probe creates return before the SSH wait so the burst's
        # other creates can land now; provisioning runs beside them
        threading.Thread(
            target=rent.provision_order,
            args=(ctx, order_id),
            kwargs={"say": say, "warn": say},
            daemon=True).start()
    count = db.execute(
        "SELECT COUNT(*) FROM experiment_rentals WHERE phase_id=?",
        (phase_id,)).fetchone()[0]
    x_min = cfg["x_min"][count % len(cfg["x_min"])]
    y_min = cfg["y_min"][count % len(cfg["y_min"])]
    quote = ctx["quote"]
    bound = _dec(quote["cost_bound_usd"])
    with db:
        if action.get("role") == "probe" or action.get("pinned"):
            # a successful rent fulfills any operator pin on the server
            db.execute(
                "DELETE FROM probe_pins WHERE phase_id=? "
                "AND server_id=?", (phase_id, sid))
        db.execute(
            "INSERT INTO experiment_rentals(order_id, phase_id, "
            "server_id, tier, reason, x_min, y_min, hourly_usd, "
            "creation_fee_usd, quoted_cost_usd, created_at, "
            "role, burst_id, mode, reused_from) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (order_id, phase_id, sid, action["tier"], action["reason"],
             x_min, y_min, str(quote["hourly_cost_usd"]),
             str(quote["creation_fee_usd"]),
             str(quote["cost_bound_usd"]), _iso(now),
             action.get("role", "slot"), action.get("burst_id"),
             mode, (reuse or {}).get("order_id")))
    detail = {"tier": action["tier"], "reason": action["reason"],
              "role": action.get("role", "slot"), "hours": hours,
              **({"pinned": True} if action.get("pinned") else {}),
              "hourly_usd": str(quote["hourly_cost_usd"]),
              "x_min": x_min, "y_min": y_min,
              "quoted_cost_usd": str(quote["cost_bound_usd"]),
              "mode": mode,
              "remaining_after": str(remaining - bound)}
    if reuse is not None:
        detail["reuse"] = {"from_order": reuse["order_id"],
                           "alias": reuse["alias"],
                           "donor_ended_min": round(
                               (now - reuse["ended_at"])
                               .total_seconds() / 60, 1)}
    if mode == "spot":
        detail.update(spot_bid_usd_day=spot_bid,
                      spot_floor_usd_day=spot_floor,
                      spot_discount=spot_discount)
        if spot_discovered:
            detail["spot_discovered"] = True
    elif spot_fallback:
        detail["spot_fallback"] = True
    emit(db, now, phase_id, order_id, sid, "rent", detail, say=say)


def _rental_value_events(db, phase, cfg, now, say=print):
    """Backfill payout-weighted value for ended rentals: once a pool
    snapshot exists >= 15 min after ended_at, attribute the wallet's
    counted payouts to the rental's worker alias and emit one
    `rental_value` event per order. Phase-1 rentals predate pool
    snapshots; their shares land in pre_snapshot_shares."""
    phase_id = phase["phase_id"]
    for row in db.execute(
            "SELECT e.*, f.alias FROM experiment_rentals e"
            " LEFT JOIN fleet_rentals f ON f.order_id = e.order_id"
            " WHERE e.phase_id = ? AND e.ended_at IS NOT NULL"
            " AND e.role <> 'weight'"
            " AND e.order_id NOT IN (SELECT order_id FROM"
            " experiment_events WHERE kind = 'rental_value'"
            " AND order_id IS NOT NULL)", (phase_id,)).fetchall():
        alias = row["alias"]
        if not alias:
            continue
        eligible_at = _iso(
            _dt(row["ended_at"]) + timedelta(minutes=15))
        if db.execute(
                "SELECT 1 FROM pool_snapshots WHERE source_ts >= ?"
                " LIMIT 1", (eligible_at,)).fetchone() is None:
            continue
        value = pool_stats.rental_value(db, alias)
        if value is None:
            continue
        cost = float(rental_cost_usd(row, cfg))
        shares = value["shares"]
        weighted = value["weighted"]
        usd = value["usd"]
        confidence = value["confidence"]
        baseline = None
        if _row_opt(row, "reused_from") is not None:
            # the alias's counted sol is cumulative across stints:
            # this rental earned only what arrived after its donor
            # handed the identity off. Split at the last snapshot
            # taken before this rental existed; without one the two
            # stints can't be separated and the total is reported
            # as merged rather than misattributed.
            base_row = db.execute(
                "SELECT w.sol FROM pool_worker_snapshots w"
                " JOIN pool_snapshots s ON s.id=w.snapshot_id"
                " WHERE w.alias=? AND s.source_ts<=?"
                " ORDER BY s.source_ts DESC LIMIT 1",
                (alias, row["created_at"])).fetchone()
            baseline = base_row["sol"] if base_row else None
            if baseline is not None and shares is not None:
                shares = max(0.0, shares - baseline)
                wps = value["weight_per_share"]
                weighted = shares * wps if wps is not None else None
                usd = (weighted * value["usd"] / value["weighted"]
                       if weighted is not None and value["weighted"]
                       and value["usd"] is not None else None)
            else:
                confidence = "merged"
        emit(db, now, phase_id, row["order_id"], row["server_id"],
             "rental_value",
             {"shares": shares,
              "weighted": (round(weighted, 4)
                           if weighted is not None else None),
              "usd_est": usd,
              "cost_usd": round(cost, 4),
              "return_x": (round(usd / cost, 3)
                           if usd is not None and cost > 0 else None),
              "weight_per_share": value["weight_per_share"],
              "clean_frac": value["clean_frac"],
              "method": value["method"],
              "confidence": confidence,
              "as_of": value["as_of"],
              "baseline_shares": baseline,
              "pre_snapshot_shares":
                  value["pre_snapshot_shares"]},
             say=say)


def _recheck_attempts(db, env_file, phase_id, now, cfg, say):
    """Re-poll the live order list for parked rent attempts.

    A POST whose reconcile could not find the new order parks
    'unknown' for safety -- the order may exist and double-renting
    must be impossible -- and 'creating' is what a runner dead
    mid-attempt leaves behind. Both block all actions through
    pending_attempts, so one transient API failure would otherwise
    freeze the runner until manual reconciliation. Parked attempts
    past attempt_recheck_min are re-queried at that interval:
    attempt_recheck_clear consecutive clean fetches with no new order
    on the server mean the POST provably created nothing, and the
    attempt auto-resolves to 'no_order'. A found candidate keeps it
    parked -- adopting an ambiguous order stays a manual decision.
    """
    gap = _minutes(cfg["attempt_recheck_min"])
    due = [p for p in fleet_store.pending_attempts(db)
           if now - _dt(p["created_at"]) >= gap
           and (p["last_recheck_at"] is None
                or now - _dt(p["last_recheck_at"]) >= gap)]
    if not due:
        return
    try:
        api_key, _ = rent.load_secrets(env_file, need_creds=False)
        current = rent.my_orders(api_key)
    except Exception as exc:
        # a failed fetch is no evidence -- keep the clean tally but
        # throttle retries to the recheck interval
        for p in due:
            fleet_store.note_attempt_recheck(
                db, p["attempt_id"], p["recheck_count"], now)
        say(f"{_iso(now)} note attempt recheck skipped: "
            f"{type(exc).__name__}: {exc}")
        return
    for p in due:
        before = set(json.loads(p["before_ids_json"]))
        found = rent.find_new_orders(current, before, p["server_id"])
        aid, sid = p["attempt_id"], p["server_id"]
        if found:
            fleet_store.note_attempt_recheck(db, aid, 0, now)
            emit(db, now, phase_id, None, sid, "attempt_recheck",
                 {"attempt_id": aid, "outcome": "candidate",
                  "order_ids": [o["id"] for o in found]}, say=say)
            continue
        count = p["recheck_count"] + 1
        if count >= cfg["attempt_recheck_clear"]:
            fleet_store.finish_attempt(db, aid, "no_order")
            emit(db, now, phase_id, None, sid, "attempt_recheck",
                 {"attempt_id": aid, "outcome": "cleared_no_order",
                  "fetches": count}, say=say)
        else:
            fleet_store.note_attempt_recheck(db, aid, count, now)
            emit(db, now, phase_id, None, sid, "attempt_recheck",
                 {"attempt_id": aid, "outcome": "clean",
                  "recheck_count": count}, say=say)


def run_cycle(db, db_path, env_file, phase, cfg, live, now=None,
              say=print):
    now = now or utcnow()
    if live:
        _recheck_attempts(db, env_file, phase["phase_id"], now, cfg,
                          say)
    state = build_state(db, phase, now, cfg, say=say)
    try:
        _rental_value_events(db, phase, cfg, now, say=say)
    except Exception as exc:
        say(f"{_iso(now)} note rental_value skipped:"
            f" {type(exc).__name__}")
    checks = [{"type": "blacklist", "order_id": oid, "server_id": sid,
               "reason": reason, "detail": detail}
              for oid, sid, reason, detail in
              blacklist_checks(db, state, now, cfg)]
    state["excluded"].update(a["server_id"] for a in checks)
    # decide()'s note channel is the decision rationale (who was
    # considered, why a pick won or nothing did); persist it as `note`
    # events so it reaches the TUI log, deduping steady-state repeats
    # so a stuck market doesn't write the same line every cycle
    notes = []
    prefix = f"{_iso(now)} note "

    def _note_say(msg):
        notes.append(
            msg[len(prefix):] if msg.startswith(prefix) else msg)
        if say:
            say(msg)

    acts = checks + decide(now, state, cfg, say=_note_say)
    # skip a cycle whose notes are identical to the previous one:
    # decisions repeat every cycle while a market stalls, the log
    # only needs them when they change
    prev_texts = [
        json.loads(r["detail_json"]).get("text")
        for r in db.execute(
            "SELECT detail_json FROM experiment_events WHERE"
            " phase_id=? AND kind='note' ORDER BY id DESC LIMIT ?",
            (phase["phase_id"], len(notes) or 1))][::-1]
    if notes != prev_texts:
        for text in notes:
            emit(db, now, phase["phase_id"], None, None, "note",
                 {"text": text}, say=say)
    # trainer version bumps: a distinct `trainer` event per observed
    # (from -> to) transition per rental -- the version string alone
    # can't tell a hotfix from an algo change, but the timestamp in
    # the log is the evidence that correlates with share-rate moves
    for r in state["rentals"]:
        for ch in r.get("trainer_changes") or []:
            seen = db.execute(
                "SELECT 1 FROM experiment_events WHERE phase_id=? "
                "AND order_id=? AND kind='trainer' AND "
                "json_extract(detail_json, '$.from')=? AND "
                "json_extract(detail_json, '$.to')=? LIMIT 1",
                (phase["phase_id"], r["order_id"],
                 ch["from"], ch["to"])).fetchone()
            if seen is None:
                emit(db, ch["at"], phase["phase_id"], r["order_id"],
                     r["server_id"], "trainer",
                     {"from": ch["from"], "to": ch["to"]}, say=say)
    # per-cycle heartbeat: silent runs are indistinguishable from a
    # dead runner otherwise
    bits = [f"{r['order_id']}:{r['role']} "
            f"{next_step(r, now, cfg)} its={r['its_total']}"
            for r in state["rentals"]]
    for p in state["pending_rerents"]:
        bits.append(f"rerent s{p['server_id']} due "
                    f"{_countdown(p['due_at'], now)}")
    for p in state["pending_bursts"]:
        bits.append(f"retry for {p['for_order']} due "
                    f"{_countdown(p['due_at'], now)}")
    for ub in state["unjudged_bursts"]:
        bits.append(f"burst {ub['burst_id']}({ub['trigger']}) "
                    f"{len(state['burst_probes'][ub['burst_id']])}"
                    f"/{ub['n_target']} probes")
    if state.get("lull_until") is not None \
            and now < state["lull_until"]:
        bits.append("lull pause "
                    f"{_countdown(state['lull_until'], now)}")
    pacing = pace(state, now, cfg)
    opt_txt = (f"opt=${pacing['sustainable_hr']:.3f}/h "
               f"head=${pacing['headroom_hr']:.3f}/h")
    vm = state.get("value_model") or {}
    vm_txt = ""
    if vm.get("source") not in (None, "unavailable"):
        vps = (vm["w_bar"] or 0.0) * (vm["P"] or 0.0)
        vm_txt = (f" v=${vps:.4f}/sh S={vm['S']:.0f}/h"
                f" beta={vm['beta']}({vm['source']},provisional)")
    tick = (f"{_iso(now)} tick committed=${state['committed']:.2f} "
            f"remaining=${state['remaining']:.2f} "
            f"burn=${float(state['active_burn_hr']):.3f}/h {opt_txt}"
            f"tgt={state['slot_target']}{vm_txt} "
            f"{' | '.join(bits) if bits else 'idle'}")
    say(tick)
    # persist the same heartbeat (throttled): a healthy steady-state
    # fleet emits no other events, and silence in the log is
    # indistinguishable from a dead runner
    _emit_throttled(
        db, now, phase["phase_id"], None, None, "tick",
        {"pulse": 1,
         "committed": f"{state['committed']:.2f}",
         "remaining": f"{state['remaining']:.2f}",
         "burn_hr": f"{float(state['active_burn_hr']):.3f}",
         "optimal_hr": f"{pacing['sustainable_hr']:.3f}",
         "headroom_hr": f"{pacing['headroom_hr']:.3f}",
         "slot_target": state["slot_target"],
         "rigs": " | ".join(bits) if bits else "idle",
         "value_model": vm_txt.strip() or None},
        "pulse", 1, say=lambda *_: None)
    execute(db, db_path, env_file, phase, cfg, acts, now, live,
            say=say)


# ---------------------------------------------------------------- CLI

def _add_db_arg(parser):
    parser.add_argument("--db", default=DEFAULT_DB, help="SQLite path")


def _config_from_args(args):
    cfg = dict(DEFAULT_CONFIG)
    for key in ("max_rentals", "hours", "x_min", "y_min"):
        value = getattr(args, key, None)
        if value is not None:
            cfg[key] = value
    for key in ("max_price_hr", "startup_min",
                "startup_restart_every_min", "fault_check_min",
                "free_window_min", "dry_after_min",
                "quote_reject_min", "unreachable_min",
                "min_reliability", "rerent_grace_min", "stale_obs_sec",
                "stale_market_min", "burst_max", "burst_fill_min",
                "probe_judge_min", "probe_cancel_by_min",
                "upgrade_cooldown_min", "its_window_min",
                "default_shares_per_hour", "spot_outbid_min",
                "spot_probe_judge_min"):
        value = getattr(args, key, None)
        if value is not None:
            cfg[key] = value
    for key in ("promote_its_floor", "promote_margin", "upgrade_margin",
                "its_exponent", "min_value_ratio", "min_return",
                "spot_min_discount", "spot_probe_min_discount",
                "spot_enabled", "loss_mining"):
        value = getattr(args, key, None)
        if value is not None:
            cfg[key] = value
    if isinstance(cfg["x_min"], str):
        cfg["x_min"] = [int(v) for v in cfg["x_min"].split(",")]
    if isinstance(cfg["y_min"], str):
        cfg["y_min"] = [int(v) for v in cfg["y_min"].split(",")]
    return validate_config(cfg)


def cmd_start_phase(args):
    db = open_db(args.db)
    try:
        now = utcnow()
        existing = db.execute(
            "SELECT phase_id, ends_at FROM experiment_phases WHERE "
            "closed_at IS NULL AND ends_at > ?",
            (_iso(now),)).fetchone()
        if existing is not None:
            print(f"phase {existing['phase_id']} is still open "
                  f"(ends {existing['ends_at']})", file=sys.stderr)
            return 2
        cfg = _config_from_args(args)
        budget = _dec(args.budget_usd)
        if budget <= 0:
            print("--budget-usd must be positive", file=sys.stderr)
            return 2
        ends_at = now + timedelta(hours=args.phase_hours)
        with db:
            db.execute(
                "INSERT INTO experiment_phases(started_at, ends_at, "
                "budget_usd, reserve_usd, config_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (_iso(now), _iso(ends_at), str(budget),
                 str(_dec(args.reserve_usd)),
                 json.dumps(cfg, sort_keys=True)))
        print(f"phase started: {_iso(now)} -> {_iso(ends_at)} "
              f"budget ${budget} reserve ${args.reserve_usd}")
        print(json.dumps(cfg, indent=2, sort_keys=True))
        return 0
    finally:
        db.close()


def cmd_run(args):
    db = open_db(args.db)
    try:
        phase = open_phase(db, utcnow())
        if phase is None:
            print("no open experiment phase; run start-phase first",
                  file=sys.stderr)
            return 2
        cfg = validate_config(json.loads(phase["config_json"]))
        if args.live:
            if args.confirm_budget is None or _dec(
                    args.confirm_budget) != _dec(phase["budget_usd"]):
                print("--live requires --confirm-budget equal to the "
                      f"phase budget ({phase['budget_usd']})",
                      file=sys.stderr)
                return 2
        lock_path = os.path.expanduser(args.db) + ".experiment.lock"
        lock_file = open(lock_path, "w")
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print(f"another run holds {lock_path}", file=sys.stderr)
            return 2
        stop = []

        def _stop(*_a):
            stop.append(True)

        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGTERM, _stop)
        mode = "LIVE" if args.live else "dry-run"
        print(f"run: phase {phase['phase_id']} ({mode}, "
              f"budget {phase['budget_usd']})")
        try:
            while not stop:
                try:
                    cfg = _live_cfg(db, phase, cfg)
                    run_cycle(db, os.path.expanduser(args.db),
                              os.path.expanduser(args.env_file), phase,
                              cfg, live=args.live)
                except Exception as exc:
                    # one bad cycle must not kill the loop; never log
                    # exception text (may carry secrets)
                    try:
                        _emit_throttled(
                            db, utcnow(), phase["phase_id"], None,
                            None, "error",
                            {"error": type(exc).__name__},
                            "error", type(exc).__name__)
                    except Exception:
                        pass
                if args.once:
                    break
                time.sleep(args.interval)
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)
            lock_file.close()
        return 0
    finally:
        db.close()


def cmd_burst(args):
    db = open_db(args.db)
    try:
        phase = open_phase(db, utcnow())
        if phase is None:
            print("no open experiment phase; run start-phase first",
                  file=sys.stderr)
            return 2
        cfg = validate_config(json.loads(phase["config_json"]))
        n = args.n if args.n is not None else cfg["burst_max"]
        if not 1 <= n <= cfg["burst_max"]:
            print(f"--n must be 1..{cfg['burst_max']}",
                  file=sys.stderr)
            return 2
        emit(db, utcnow(), phase["phase_id"], None, None,
             "burst_requested", {"n": n})
        return 0
    finally:
        db.close()


def _cfg_value(key, raw):
    """CLI text -> the config key's own type."""
    default = DEFAULT_CONFIG[key]
    if isinstance(default, bool):
        if raw.lower() in ("on", "true", "1", "yes"):
            return True
        if raw.lower() in ("off", "false", "0", "no"):
            return False
        raise ValueError(f"{key} expects on/off, got {raw!r}")
    if isinstance(default, list):
        return [type(default[0])(v) for v in raw.split(",")]
    if isinstance(default, int):
        return int(raw)
    if isinstance(default, float):
        return float(raw)
    return raw


def cmd_set(args):
    db = open_db(args.db)
    try:
        updates = {}
        try:
            for pair in args.kv:
                key, sep, raw = pair.partition("=")
                if key == "strategy" and sep:
                    # named bundle: expands to the bundled keys and
                    # overwrites them; keys set later in the same
                    # command (or afterwards) still win
                    if raw not in STRATEGIES:
                        raise ValueError(
                            f"unknown strategy {raw!r} (want one of: "
                            + ", ".join(sorted(STRATEGIES)) + ")")
                    updates.update(STRATEGIES[raw])
                    continue
                if not sep or key not in DEFAULT_CONFIG:
                    raise ValueError(
                        f"unknown config key {key!r} (want key=value "
                        "or strategy=<name>)")
                updates[key] = _cfg_value(key, raw)
            cfg = set_phase_config(db, **updates)
        except ValueError as exc:
            print(f"invalid config: {exc}", file=sys.stderr)
            return 2
        if cfg is None:
            print("no open experiment phase", file=sys.stderr)
            return 2
        print(" ".join(f"{k}={cfg[k]}" for k in updates))
        if "score" in updates:
            print(SCORE_HELP[cfg["score"]])
        return 0
    finally:
        db.close()


def cmd_reset_tried(args):
    db = open_db(args.db)
    try:
        phase = open_phase(db, utcnow())
        if phase is None:
            print("no open experiment phase; run start-phase first",
                  file=sys.stderr)
            return 2
        emit(db, utcnow(), phase["phase_id"], None, None,
             "tried_reset", {})
        return 0
    finally:
        db.close()


def cmd_status(args):
    db = open_db(args.db)
    try:
        now = utcnow()
        phase = open_phase(db, now)
        if phase is None:
            print("no open experiment phase")
            return 0
        cfg = validate_config(json.loads(phase["config_json"]))
        state = build_state(db, phase, now, cfg, apply_endings=False,
                            say=lambda *_a: None)
        print(f"phase {phase['phase_id']}: {_iso(now)} -> "
              f"{phase['ends_at']} budget ${state['budget']} "
              f"committed ${state['committed'].quantize(Decimal('0.01'))} "
              f"remaining ${state['remaining'].quantize(Decimal('0.01'))}")
        for r in state["rentals"]:
            print(f"  order {r['order_id']} server {r['server_id']} "
                  f"[{r['tier']}/{r['role']}] "
                  f"age {str(r['age']).split('.')[0]} "
                  f"t0 {_iso(r['t0'])} obs_age "
                  f"{str(r['obs_age']).split('.')[0] if r['obs_age'] else '-'} "
                  f"its={r['its_total']} shares "
                  f"{sum(a for _, a in r['share_arrivals'])} "
                  f"last_share {_iso(r['last_share_at'])}")
        for pending in state["pending_rerents"]:
            print(f"  pending rerent: server {pending['server_id']} "
                  f"due {_iso(pending['due_at'])}")
        for b in state["unjudged_bursts"]:
            print(f"  burst {b['burst_id']} ({b['trigger']}) unjudged "
                  f"since {_iso(b['started_at'])}: "
                  f"{len(state['burst_probes'][b['burst_id']])}"
                  f"/{b['n_target']} probes")
        return 0
    finally:
        db.close()


def cmd_report(args):
    db = open_db(args.db)
    try:
        if args.phase is not None:
            phase = db.execute(
                "SELECT * FROM experiment_phases WHERE phase_id=?",
                (args.phase,)).fetchone()
        else:
            phase = db.execute(
                "SELECT * FROM experiment_phases ORDER BY phase_id DESC "
                "LIMIT 1").fetchone()
        if phase is None:
            print("no experiment phase")
            return 2
        cfg = validate_config(json.loads(phase["config_json"]))
        _report(db, phase, cfg)
        return 0
    finally:
        db.close()


def _report(db, phase, cfg):
    now = utcnow()
    phase_id = phase["phase_id"]
    listings = server_listings(db)
    rental_values = {
        r["order_id"]: json.loads(r["detail_json"])
        for r in db.execute(
            "SELECT order_id, detail_json FROM experiment_events"
            " WHERE kind='rental_value' AND order_id IS NOT NULL")}
    xy = {}
    for row in db.execute(
            "SELECT * FROM experiment_rentals WHERE phase_id=? "
            "ORDER BY order_id", (phase_id,)):
        order_id = row["order_id"]
        good = _observations(db, order_id)
        arrivals = share_arrivals(good)
        shares = sum(a for _, a in arrivals)
        created, ended = _dt(row["created_at"]), _dt(row["ended_at"])
        span_end = ended or now
        hours = max(0.0, (span_end - created).total_seconds() / 3600.0)
        cost = _dec(row["hourly_usd"]) * Decimal(str(hours))
        if ended and (ended - created) > _minutes(cfg["free_window_min"]):
            cost += _dec(row["creation_fee_usd"])
        if row["ended_at"] is None:
            cost = _dec(row["quoted_cost_usd"])
        obs_hours = ((good[-1][0] - good[0][0]).total_seconds() / 3600.0
                     if good else 0.0)
        cpu_its = [s["its"]["cpu"] for _, s in good
                   if (s.get("its") or {}).get("cpu") is not None]
        gpu_its = [s["its"]["gpu"] for _, s in good
                   if (s.get("its") or {}).get("gpu") is not None]
        restarts = _restart_events(db, phase_id, order_id)
        segments = trees.tree_segments(db, order_id)
        seg_shares = []
        for seg in segments:
            start, end = _dt(seg["start_ts"]), _dt(seg["end_ts"])
            seg_shares.append(sum(
                a for ts, a in arrivals
                if ts >= start and (end is None or ts < end)))
        tree_changed = 0
        share_after = 0
        for ts in restarts:
            # a different tree id within 3 min of the restart
            before_seg = next(
                (s for s in segments
                 if _dt(s["start_ts"]) <= ts and
                 (s["end_ts"] is None or _dt(s["end_ts"]) > ts)), None)
            if any(s["tree_id"] != (before_seg or {}).get("tree_id")
                   and ts < _dt(s["start_ts"]) <= ts + _minutes(3)
                   for s in segments):
                tree_changed += 1
            if any(t <= ts + _minutes(5) and t >= ts
                   for t, _ in arrivals):
                share_after += 1
        listing = listings.get(row["server_id"]) or {}
        cpu = (listing.get("specs") or {}).get("cpu") or "-"
        gpu = market.offered_gpu(listing) or "-"
        rv = rental_values.get(order_id)
        rv_txt = ""
        if rv is not None:
            est = rv.get("usd_est")
            ret = rv.get("return_x")
            rv_txt = (
                f" est$={f'{est:.4f}' if est is not None else '-'}"
                f" ret={f'{ret:.1f}x' if ret is not None else '-'}")
        print(f"order {order_id} server {row['server_id']} "
              f"[{row['tier']}] cpu={cpu} gpu={gpu} x={row['x_min']} "
              f"y={row['y_min']} observed={obs_hours:.2f}h "
              f"cost=${cost.quantize(Decimal('0.01'))} shares={shares} "
              f"sh/USD="
              f"{(Decimal(shares) / cost).quantize(Decimal('0.01')) if cost else '-'} "
              f"its(cpu med={_median(cpu_its)} gpu med={_median(gpu_its)}) "
              f"tree_changed_3m={tree_changed} "
              f"share_5m={share_after}{rv_txt}")
        for seg, seg_count in zip(segments, seg_shares):
            print(f"    tree {seg['tree_id']} epoch {seg['epoch']} "
                  f"{seg['start_ts']} -> {seg['end_ts'] or 'open'} "
                  f"shares={seg_count}")
        xy.setdefault(("x", row["x_min"]), []).append((shares, cost))
        xy.setdefault(("y", row["y_min"]), []).append((shares, cost))
    if rental_values:
        print("Per-rental values are provisional allocations, "
              "not measured payouts.")
    for burst in db.execute(
            "SELECT * FROM experiment_bursts WHERE phase_id=? "
            "ORDER BY burst_id", (phase_id,)):
        print(f"burst {burst['burst_id']} ({burst['trigger']}) "
              f"started {burst['started_at']} "
              f"judged {burst['judged_at'] or '-'}")
        first_trees = set()
        for prow in db.execute(
                "SELECT * FROM experiment_rentals WHERE burst_id=? "
                "ORDER BY order_id", (burst["burst_id"],)):
            p_order = prow["order_id"]
            p_good = _observations(db, p_order)
            p_arrivals = share_arrivals(p_good)
            p_t0 = p_good[0][0] if p_good else None
            judge_shares = sum(
                a for ts, a in p_arrivals
                if p_t0 is not None and ts >= p_t0
                and ts <= p_t0 + _minutes(
                    _probe_judge_min(_row_mode(prow), cfg)))
            p_shares = sum(a for _, a in p_arrivals)
            p_segs = trees.tree_segments(db, p_order)
            first_tree = p_segs[0]["tree_id"] if p_segs else None
            if first_tree:
                first_trees.add(first_tree)
            p_listing = listings.get(prow["server_id"]) or {}
            p_cpu = (p_listing.get("specs") or {}).get("cpu") or "-"
            p_gpu = market.offered_gpu(p_listing) or "-"
            print(f"    probe {p_order} server {prow['server_id']} "
                  f"[{prow['role']}] cpu={p_cpu} gpu={p_gpu} "
                  f"hourly=${prow['hourly_usd']} "
                  f"shares_judge={judge_shares} "
                  f"first_tree={first_tree or '-'} shares={p_shares}")
            if prow["role"] in ("kept", "control"):
                kept_cost = _dec(prow["hourly_usd"]) * Decimal(str(
                    max(0.0, (_dt(prow["ended_at"]) or now)
                        .timestamp() - _dt(prow["created_at"])
                        .timestamp()) / 3600.0))
                per_usd = (Decimal(p_shares) / kept_cost).quantize(
                    Decimal("0.01")) if kept_cost else "-"
                print(f"      {prow['role']}: shares/USD={per_usd}")
        if first_trees:
            print(f"    distinct first trees: {len(first_trees)}")
    for (axis, value), rows in sorted(xy.items()):
        total_shares = sum(s for s, _ in rows)
        total_cost = sum(c for _, c in rows)
        per_usd = (Decimal(total_shares) / total_cost).quantize(
            Decimal("0.01")) if total_cost else "-"
        print(f"summary {axis}={value}: rentals={len(rows)} "
              f"shares={total_shares} cost=${total_cost.quantize(Decimal('0.01'))} "
              f"sh/USD={per_usd}")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Experiment-phase auto-rental runner")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("start-phase", help="open a new phase")
    sp.add_argument("--budget-usd", required=True)
    sp.add_argument("--phase-hours", type=float, default=48)
    sp.add_argument("--reserve-usd", default="0.50")
    sp.add_argument("--max-rentals", type=int)
    sp.add_argument("--hours", type=int)
    sp.add_argument("--max-price-hr")
    sp.add_argument("--x-min")
    sp.add_argument("--y-min")
    sp.add_argument("--startup-min", type=int)
    sp.add_argument("--startup-restart-every-min", type=int)
    sp.add_argument("--fault-check-min", type=int)
    sp.add_argument("--free-window-min", type=int)
    sp.add_argument("--dry-after-min", type=int)
    sp.add_argument("--quote-reject-min", type=int)
    sp.add_argument("--min-reliability", type=float)
    sp.add_argument("--unreachable-min", type=int)
    sp.add_argument("--rerent-grace-min", type=int)
    sp.add_argument("--stale-obs-sec", type=int)
    sp.add_argument("--stale-market-min", type=int)
    sp.add_argument("--burst-max", type=int)
    sp.add_argument("--burst-fill-min", type=int)
    sp.add_argument("--probe-judge-min", type=int)
    sp.add_argument("--probe-cancel-by-min", type=int)
    sp.add_argument("--promote-its-floor", type=float)
    sp.add_argument("--promote-margin", type=float)
    sp.add_argument("--upgrade-margin", type=float)
    sp.add_argument("--upgrade-cooldown-min", type=int)
    sp.add_argument("--its-window-min", type=int)
    sp.add_argument("--its-exponent", type=float)
    sp.add_argument("--min-value-ratio", type=float)
    sp.add_argument("--min-return", type=float)
    sp.add_argument("--default-shares-per-hour", type=int)
    sp.add_argument("--spot-enabled", default=None,
                    action=argparse.BooleanOptionalAction)
    sp.add_argument("--loss-mining", default=None,
                    action=argparse.BooleanOptionalAction)
    sp.add_argument("--spot-min-discount", type=float)
    sp.add_argument("--spot-probe-min-discount", type=float)
    sp.add_argument("--spot-outbid-min", type=int)
    sp.add_argument("--spot-probe-judge-min", type=int)
    _add_db_arg(sp)
    sp.set_defaults(func=cmd_start_phase)

    rp = sub.add_parser("run", help="run the decision loop")
    rp.add_argument("--live", action="store_true")
    rp.add_argument("--confirm-budget")
    rp.add_argument("--interval", type=float, default=15)
    rp.add_argument("--once", action="store_true")
    rp.add_argument("--env-file", default=DEFAULT_ENV)
    _add_db_arg(rp)
    rp.set_defaults(func=cmd_run)

    bp = sub.add_parser(
        "burst", help="request a probe burst (event only; decide rents)")
    bp.add_argument("--n", type=int, default=None,
                    help="probes to rent (<= phase burst_max)")
    _add_db_arg(bp)
    bp.set_defaults(func=cmd_burst)

    sc = sub.add_parser(
        "set", help="update open-phase config keys live "
                    "(e.g. loss_mining=on, score=gross, or a named "
                    "strategy preset: strategy=value/aggressive/"
                    "throughput)")
    sc.add_argument("kv", nargs="+", metavar="key=value")
    _add_db_arg(sc)
    sc.set_defaults(func=cmd_set)

    rt = sub.add_parser(
        "reset-tried",
        help="re-open the upgrade scan to servers tried so far")
    _add_db_arg(rt)
    rt.set_defaults(func=cmd_reset_tried)

    st = sub.add_parser("status", help="phase + rental state")
    _add_db_arg(st)
    st.set_defaults(func=cmd_status)

    rep = sub.add_parser("report", help="per-rental report")
    rep.add_argument("--phase", type=int)
    _add_db_arg(rep)
    rep.set_defaults(func=cmd_report)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
