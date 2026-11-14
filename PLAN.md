# clorqub — Qubic accumulation optimizer for Clore.ai

## Goal

**Accumulate as much QUBIC as possible per dollar** of Clore.ai rental spend,
mining to the **jetski pool** with **qli (qubic.li)** as a switchable secondary
pool. A slight loss versus the QUBIC market price is acceptable (QUBIC is near
its all-time low, so the aim is to stack coins, not to earn USD). Spend is
held back by a small rental limit and a daily budget, not by a profit gate.

```text
score(server, pool) = expected_qubic_per_hour(server, pool) / cost_usd_per_hour(server)
                    = QUBIC per dollar

expected_qubic_per_hour = it_per_sec(server)                 # per-rig, hardware
                        x shares_per_iteration(pool, epoch)  # per-pool, fleet-wide
                        x qubic_per_share(pool)              # per-pool, from payouts

effective_cost_per_qubic = 1 / score                         # USD per QUBIC mined
```

- `it_per_sec(server)`: that Clore server's own measured it/s when there is
  enough of it, otherwise the hardware-model prior (see "Per-server tracking").
  it/s settles within minutes, so rigs are judged on it, not on share counts,
  which are noisy.
- `shares_per_iteration(pool, epoch)`: pooled across every rig on that pool,
  which gives far more data than any single rig.
- `qubic_per_share(pool)`: from actual payouts. qli pays per share (PPS).
  jetski has solo and PPLNS endpoints (currently the same server per their
  Discord); start with a rough estimate and refine it from payouts.
- `cost_usd_per_hour`: Clore price + amortized creation fee + HiveOS
  per-worker cost (see "Clore billing").

Ranking is in QUBIC per dollar, so it doesn't depend on the QUBIC/USD price.
The market price is only used for the premium ceiling and the high-value test
below.

### Spend policy

- **Premium ceiling:** don't rent a server whose `effective_cost_per_qubic` is
  above `market_price x (1 + max_premium)`. `max_premium` is in config
  (default 0.25 = pay up to 25% more per QUBIC than buying it). This allows a slight
  loss and rules out rigs that are far worse than just buying QUBIC.
- **Rental limit:** at most `max_rentals` concurrent rentals (default **2**).
- **High-value burst:** above `max_rentals`, up to `max_rentals_burst`
  (default 4) total, only for candidates that are *high value*: either
  - `effective_cost_per_qubic <= market_price x (1 - burst_discount)` (mining
    it beats buying; default `burst_discount` 0.10), or
  - score >= `burst_score_multiple` x the median score of current rentals
    (default 1.5).

  Burst rentals are tagged. When a burst rental stops qualifying, it is the
  first to be released, even if it is still under the premium ceiling, so the
  fleet goes back to `max_rentals`.
- **Daily budget cap** (hard `$`/day, checked against actual charges) applies
  on top of all of the above, burst included.
- Market price comes from an exchange/aggregator API, cached; if it is
  unavailable, the ceiling and burst tests fail closed (no new rentals beyond
  `max_rentals`, no burst).

Poll the marketplace, rank by QUBIC per dollar, rent the best rigs within the
spend policy, boot them on HiveOS with a mining flight sheet, measure, switch
pools on rigs that aren't yielding, and rotate out underperformers.

## Decisions (from user)

- **Objective: accumulate QUBIC.** Rank by QUBIC per dollar; slight loss vs
  market price is OK; spend limited by rental count + daily budget (see
  "Spend policy").
- **Rental limit: 2 concurrent**, extendable only for high-value rentals.
- **Pools: jetski (primary) + qli/qubic.li (secondary), switchable per rig on
  yield.** jetski: `qubjetski-Client`, `wss://pplns.jtskxpool.ai/ws/`. qli:
  `qli-Client` against the qubic.li pool, always PPS. The pool is a per-worker
  setting: each pool gets its own HiveOS flight sheet, and switching pools is
  just assigning the worker the other flight sheet — the rental stays up. A
  cheap rental that isn't yielding on one pool gets tried on the other before
  we release it. Payout mode (jetski solo vs PPLNS) is not a design driver for
  now.
- **Rig OS: HiveOS image on Clore.ai.** Clore offers a HiveOS image in its web
  UI; Phase 0 confirms the same works through the order API. Rented servers
  join the user's HiveOS farm. Mining is controlled through HiveOS flight
  sheets, not through a hand-rolled Docker start command.
- **Log/stats parsing: reuse `~/qubjetski-Linux-v4.5/mining_optimizer.py`**,
  which already has proven parsers for both miners (see "Reuse" below).
- **UI: headless daemon + separate clients.** The daemon runs unattended under
  systemd. CLI, a Textual TUI, a later web view and a future Discord bot are
  all separate processes that read SQLite and send commands through it (see
  "Process model").

## Assumptions (verify in Phase 0)

- SDK: official `clore-ai` Python package (`pip install clore-ai`), auth via
  `CLORE_API_KEY` env var.
- The HiveOS image offered in Clore's web UI can also be selected through the
  order API, with the farm hash / rig id passed at order time, so a new rental
  auto-registers as a HiveOS worker.
- HiveOS API v2 (token auth) can: list workers, assign a flight sheet
  (`PATCH /farms/{farm}/workers/{worker}` `{"flight_sheet_id": X}`), return
  per-worker miner stats, and delete workers.
- qubjetski runs on HiveOS as a **custom miner** package, since it isn't a
  built-in HiveOS miner. `qli-Client` is packaged the same way (second custom
  miner, second flight sheet). Config carries one flight-sheet id per pool.
- Hardware: the jetski client supports CPU (AVX2 etc.) and GPU
  (`-gpu`, CUDA/AMD). Clore is GPU-centric, but CPU threads also produce shares,
  so a rig's score is CPU + GPU together.

## Reuse from mining_optimizer.py

`mining_optimizer.py` is **not modified**. The pure, stdlib-only functions
clorqub needs are copied into `clorqub/qubic_parse.py`, with a header comment
naming the source file so later fixes can be ported by hand:

- `STATUS_RE` / `SHARES_ONLY_RE`: log line
  `E:<n> | SHARES: a/b (R:r) | [AVX2] X it/s | Y avg it/s`, with the
  `QUBJETSKI ` prefix handled.
- Trainer version parsing for **both** miners:
  - qubjetski: `TRAINER_UPDATED_RE` (`Trainer Updated:` / `Worker started |`,
    variant + `v<epoch>.<build>`);
  - qli: `<flavor> cpu <ver> is starting` (pplns/tree) for version + flavor,
    and `Trainer: Version: AVX2` / `qli-worker-AVX2` restart lines for the
    variant.

  The version is included in the perf model so an update doesn't look like a
  performance change.
- `parse_stats_file`, `find_stats_files`, `epoch_history`: `stats.<epoch>.lock`
  JSON (`FoundShares`, `SubmittedShares`, `InvalidShares`,
  `Solutions[].Created/ReportedScore/Invalid`). Authoritative share counts.
- `stats_mirror` logic: **qli deletes its own stats files** (its embedded
  updater runs `rm /q/*.lock` after every failed download attempt, 670+ times
  in the local `/var/qli.error.log`). qli rigs copy changed `.lock` files to a
  safe dir, and all reads use the mirror.
- `count_shares_in_window`, the two-newest-epoch merge (so an epoch rollover
  doesn't drop shares or look like a stall), and the within-run inter-share gap
  median (including the `times[-65:]` slicing fix).
- `tail_log_lines`, `read_new_log_lines`, `ANSI_RE`: incremental log tailing.
- Adaptive stall logic (`stall_gap_multiplier` x median gap, clamped): reused as
  the "this rental is dead / underperforming" detector in `rotate.py`.

Not reused: the curses TUI, `ServiceManager`/sudo/systemctl handling, the
thread-mode switching, and the port-31111 dual-miner workaround (one miner per
rented rig).

## Architecture

Python. Headless daemon + clients, SQLite (WAL mode) for state.

```text
clorqub/
  config.toml          # max_rentals / burst settings, max_premium, budget cap,
                       #   intervals, farm id, per-pool flight-sheet ids
                       #   (jetski + qli), thresholds
  hive/qubjetski/      # HiveOS custom miner package (tar.gz served to rigs)
    h-manifest.conf    #   name, version, log path
    h-config.sh        #   render appsettings.json from flight-sheet vars
    h-run.sh           #   exec qubjetski-Client -start
    h-stats.sh         #   parse log/stats -> khs, ar[acc,rej], uptime JSON
  hive/qubicli/        # same packaging for qli-Client -> qubic.li pool, plus
                       #   the .lock stats mirror
  clorqub/
    qubic_parse.py     # parsers copied from mining_optimizer.py (original untouched)
    market.py          # Clore marketplace polling (CPU + GPU specs, price)
    price.py           # QUBIC/USD and CLORE/USD market price (cached)
    perf.py            # it/s per server (history blended with hardware prior:
                       #   cpu model, gpu model, trainer ver); per-pool
                       #   shares/iteration and QUBIC/share
    scorer.py          # QUBIC-per-dollar ranking + spend policy (ceiling,
                       #   rental limit, burst)
    rentals.py         # Clore order lifecycle: rent, early check, release
    hive.py            # HiveOS API: find worker, assign/switch flight sheet
                       #   (= switch pool), read stats, delete worker
    collector.py       # poll HiveOS/pool/rig stats -> local SQLite snapshots
    stats.py           # derive share events, windows and rates from snapshots
    rotate.py          # drop/replace + pool-switch logic (adaptive stall,
                       #   yield vs expected, hysteresis, burst unwind)
    store.py           # SQLite schema + access; the only writer is the daemon
    daemon.py          # main loop; executes queued commands
    watchdog.py        # dead-man switch (separate process)
    cli.py             # status (share windows) / report / dry-run / pause /
                       #   set-budget / set-max-rentals / servers /
                       #   server <id> / blacklist add|rm|list /
                       #   switch-pool / release
    tui.py             # Textual viewer (Phase 5)
```

### Process model

The daemon spends money unattended, so it is never tied to a terminal: it runs
headless under systemd. Every UI is a separate client process.

- **The daemon is the only writer** of rentals, stats snapshots and decisions.
  SQLite WAL mode lets clients read while it writes.
- **Clients read SQLite.** They can start, stop or crash without affecting the
  daemon, and the daemon needs no API or socket server.
- **Control goes through a `commands` table.** Clients insert rows (pause,
  resume, set-budget, set-max-rentals, blacklist, release, switch-pool, ...);
  the daemon picks them up each cycle, executes them, records the result on
  the row, and logs them in the decision log.
  - Each row has: `id`, `command`, `args` JSON, `source` (cli / tui / web /
    discord), `actor` (local user or Discord user id), `created_at`, `status`
    (pending / done / rejected / expired), `result`, `executed_at`.
  - The daemon validates every command itself (budget cap, rental limit,
    dry-run, actor allowlist per source) rather than trusting the client.
  - Commands that increase spend (raise budget, raise `max_rentals`,
    force-rent) need a confirmation step, recorded as a second row or a
    `confirmed_by` field, so a remote client like Discord can't spend from a
    single message.
- **Outbound events go through an `events` table** (outbox): rentals, burst
  rentals, releases, pool switches, blacklist adds, stalls, budget hit,
  errors, daily summary (QUBIC mined, USD spent, effective cost per QUBIC vs
  market). Each event has a severity and per-consumer delivery tracking, so a
  client that was down catches up instead of missing events.

Clients:

- **CLI** (Phases 1–4): one-shot commands.
- **Textual TUI** (Phase 5): fleet table, per-worker share windows, server
  drill-down, decision log, command entry. Runs locally or over SSH.
- **Web view** (later): FastAPI reading the same file, bound to localhost or
  behind auth.
- **Discord bot** (future phase, user already has a server): posts `events`
  and accepts commands from allowlisted Discord user ids in designated
  channels. Same `commands` / `events` path as every other client, so no
  daemon changes are needed to add it. The bot token is an env var.

### Clore billing

- **Creation fee:** Clore charges a creation fee once a rental passes 10
  minutes: **$0.01 for spot, $0.10 for on-demand**. A rental cancelled inside
  the first 10 minutes is free (confirm the exact rule in Phase 0). The fees
  are in config (`creation_fee_spot`, `creation_fee_on_demand`), not hardcoded.
  - These are small next to hourly prices, so the fee matters mostly for
    churn: a few cents per swap. The bigger cost of replacing a rig is the
    unpaid boot and trainer-download time.
  - Since the spot fee is tiny, a hardware mismatch or dead rig on spot is
    still cancelled early, but mainly to avoid paying for useless time.
- **Early boot check (inside the free window):** after ordering, the daemon
  checks quickly. If any of these fail by about the 8-minute mark, it cancels
  before the fee applies:
  - the HiveOS worker appeared;
  - the flight sheet is applied and the miner is running;
  - it/s > 0;
  - the reported CPU/GPU matches the listing;
  - the network is usable: the trainer download is progressing and the miner
    connected to the pool.
  Trainer download can be slow, so the cutoff is configurable, and the
  rule is "miner started and hardware matches", not "shares produced".

### Network / region filter

Servers in **Russia and Ukraine** often have major bandwidth problems
(slow trainer downloads, dropped pool connections).

- `country_blocklist` in config, default `["RU", "UA"]`: these servers are
  dropped before scoring, like blacklisted servers. Editable via config or a
  command.
- Clore shows country and network speed, but the **listed speed is not
  reliable** (update frequency unknown). `min_network_mbps` is only a loose
  pre-filter for very low listings, off by default.
- **Measured trainer download time is the real signal.** Outside Russia the
  download is almost instant; Russian rentals often take several minutes. The
  collector records download time per rental, server and country.
  - Early boot check: a download still not finished after
    `max_trainer_download_seconds` (config, default ~120s) fails the check, so
    the rental is cancelled inside the free window.
  - A server's measured download time is kept and used for future scoring,
    replacing its listed speed.
- Per-country stats (early-check failures, pool disconnects, trainer download
  time, stalls) are kept in SQLite, so `cli.py report` can show other regions
  that do badly; adding them to the blocklist is a manual decision.
- Network problems are an auto-blacklist reason for the server itself, even
  outside blocked countries.
- The creation fee is a sunk cost once paid. Scoring amortizes it over the
  expected rental length; rotation counts it as a cost of replacing a rig.
- **Spend tracking** uses what Clore actually charged (fees + hourly), not
  price x hours. Clore prices in CLORE/BTC, so USD cost moves with the
  exchange rate; store both the native amount and the USD rate at the time.
- Every order is placed with a **maximum duration** (where the API allows), so
  a rental can't bill forever if everything else fails.

### Local stats capture

HiveOS and the miner reset counters when the flight sheet changes (pool
switch), the miner restarts, or the worker is deleted, and a pool only knows
about shares sent to it. clorqub therefore keeps **its own** history in
SQLite, keyed by rental, and never relies on a remote counter staying alive.

- `collector.py` polls every active worker on a fixed cadence and stores a
  **raw snapshot** (source, timestamp, rental, worker, pool, flight sheet id,
  raw JSON) plus parsed fields: it/s, accepted/rejected/found/invalid
  counters, uptime, trainer version, epoch.
- **Counter resets:** a counter that goes down means a reset (switch,
  restart, rollover). The delta is the new value, not a negative number.
  Share events are derived from these deltas.
- **Final snapshot before any switch or release:** before the daemon changes a
  worker's flight sheet, releases a rental or deletes a HiveOS worker, it takes
  one last snapshot (HiveOS stats, plus the rig's `stats.<epoch>.lock` over SSH
  if reachable), so the tail of a run isn't lost.
- Pool-side stats (jetski / qubic.li APIs, by worker name) are stored as
  separate snapshot sources, so rig-reported and pool-reported numbers can be
  compared.
- Raw snapshots are kept (with a configurable retention) so derived tables
  can be recomputed if a parser bug is fixed.

### Per-server tracking and blacklist

Every rental is keyed by its **Clore server id**, so we can see how each
physical server does over time, across rentals.

SQLite tables:

- `servers`: clore `server_id` (plus host/owner id if Clore exposes one),
  country, listed network speed, hardware specs as listed, first/last seen,
  price history.
- `rentals`: clore order id, `server_id`, HiveOS worker id, start/end, price,
  creation fee, actual charges, spot/on-demand, `burst` flag, end reason
  (rotated, stalled, interrupted, budget, burst-unwind, early-check failed,
  manual).
- `pool_runs`: one row per (rental, pool) stretch: start/end, flight sheet id,
  why it started (initial, switch) and ended. Every switch closes one run and
  opens the next, so per-pool yield is attributable.
- `snapshots`: raw collector snapshots (see "Local stats capture").
- `measurements`: `rental_id`, `server_id`, `pool_run_id`, `pool`, timestamp,
  epoch, trainer version, it/s, found/submitted/invalid shares, derived from
  snapshots.
- `shares`: individual share events (`worker`, `server_id`, `rental_id`,
  `pool_run_id`, `pool`, `Created` UTC, epoch, invalid, `ReportedScore`),
  deduped by `SolutionHash`. These come from `stats.<epoch>.lock` `Solutions[]`.
  When only counters are available (HiveOS/pool), counter deltas between polls
  are stored as timestamped events.
- `payouts`: per pool, QUBIC amount, time, epoch, source. Feeds
  `qubic_per_share`.
- `prices`: QUBIC/USD and CLORE/USD samples, source, time.
- `blacklist`: `server_id` (or host id), reason, source (auto/manual), added
  at, optional expiry.
- `commands`, `events`, `decisions`: see "Process model".

Use:

- **Scoring**: blacklisted servers are dropped before ranking. For other
  servers, blend that server's measured it/s with the hardware prior, weighted
  by how much rented time we have on it. A known-good server can then beat an
  untested one with the same specs, and the reverse.
- **Exploration**: with only 2 slots, exploration doesn't get a dedicated slot.
  Instead, an untested server whose prior-based score is close to the best
  (within a configurable margin) may take a slot, at most one at a time, so a
  wrong prior for a hardware model gets corrected.
- **Auto-blacklist** (thresholds in config, every trigger logged with its data):
  - measured it/s < X% of the hardware prior over >= N hours of rentals;
  - never joined HiveOS, or failed the early boot check;
  - repeated stalls (the adaptive stall detector) or spot interruptions;
  - invalid/rejected share ratio above a threshold;
  - network problems: slow trainer download, repeated pool disconnects;
  - listed specs don't match what the rig reports (CPU/GPU model, thread count).
  Auto entries expire after a configurable period so a fixed server can come
  back. Manual entries never expire.
- **Reports**: `cli.py server <id>` gives a server's rental history, it/s and
  shares/hr over time per pool, QUBIC mined, effective cost per QUBIC and
  incidents. `cli.py servers` ranks every server we've rented by lifetime
  QUBIC per dollar.

### Share summary windows

Share counts for **15m / 1h / 2h / 4h / 24h / epoch** (windows in config
`summary_windows`), shown at these levels:

- **fleet**: all active rentals, plus total $/h, QUBIC/h, QUBIC per dollar and
  effective cost per QUBIC vs market price per window, split by pool;
- **per worker**: one row per HiveOS worker / pool worker name (`alias`,
  `-workername` on jetski; qli equivalent), with rental id, `server_id`,
  current pool, burst flag, hardware, it/s, uptime and $/h alongside the
  windows. Also includes workers clorqub didn't rent (e.g. your local rig) if
  HiveOS or the pool reports them, marked as unmanaged;
- **per rental / server**: windows for one rental, or rolled up across all of a
  server's rentals, split by pool run;
- **per server history**: the same windows in `cli.py server <id>`.

Each window shows found / invalid shares, shares/h, and QUBIC per dollar
(spend over the same window). Computed from the `shares` table with the
`count_shares_in_window` logic and the epoch-rollover merge. "Epoch" is the
current Qubic epoch only. A window longer than a rental's uptime is marked
partial (e.g. `12 (3h)`), so a new rig isn't compared unfairly.

Shown by `cli.py status` (fleet + per-worker table; `--by server|rental`) and
in the Phase 5 Textual TUI.

### Share data sources (in order of preference)

All sources are captured into local SQLite by `collector.py`.

1. **HiveOS API worker stats**, filled in by our `h-stats.sh` (hashrate +
   accepted/rejected shares). Centralized, no SSH. Poll slowly (rate limits).
2. **Pool APIs**: jetski per-worker stats (by `alias`/workername) and the
   qubic.li API for qli workers, where available. Independent of the rig, so
   they catch rigs that are lying or offline.
3. **`stats.<epoch>.lock` over SSH** via HiveOS/Clore SSH access (qli: the
   mirror dir): authoritative, used for audit, final pre-switch snapshots, and
   calibrating the perf model.

### Pool switching

jetski stays the default pool a new rental boots onto; qli is the alternate.
Both pools' flight sheets live in the same farm, so a switch is one HiveOS API
call (`hive.py` assigns the worker the other flight sheet id) — no rental
churn, the miner restarts under the new sheet.

Rules (per rig, on yield):

- Every measurement/share row carries its `pool_run`; a rig's yield is only
  compared **within the same pool**. Pools are compared through the common
  unit, QUBIC per hour, not raw share counts.
- **Trigger:** a rental whose measured yield on its current pool is well below
  its *expected* yield (its it/s x that pool's fleet-wide shares/iteration),
  **and whose price is still competitive** (cheap rig, bad pool fit), gets
  switched to the other pool before we consider releasing it.
- **Noise guard:** "well below" needs a minimum share count or a confidence
  bound (share arrivals are Poisson), not a raw 15m or 1h count. With only 2
  rigs, the fleet-wide shares/iteration also includes the user's local rig
  where its pool data is available.
- After a switch, a new `pool_run` starts and the rig is measured fresh; the
  warm-up period applies again before it can be judged.
- **Anti-flapping:** a minimum measurement window before a switch and a max
  number of switches per rental. qli is PPS and jetski's endpoints currently go
  to the same server, so a switch gives up little credit; these limits exist
  to keep measurements clean, not to protect payout.
- A rig where one pool fails outright (0 it/s, can't connect, trainer download
  keeps failing, GPU unsupported) is switched right away, without waiting for
  the noise guard.

### Core loop

```text
every N minutes:
  1. execute pending rows in `commands`
  2. refresh market prices (QUBIC/USD, CLORE/USD)
  3. snapshot marketplace (server_id, country, network speed, cpu/gpu specs,
     price/hr, availability); drop blacklisted servers, blocked countries and
     listings under min_network_mbps
  4. collector: snapshot every active worker into SQLite; update perf model
     (it/s per server, shares/iteration and QUBIC/share per pool)
  5. early boot check on rentals still inside the 10-minute free window;
     cancel failures before the creation fee applies
  6. score all available servers (QUBIC per dollar), apply premium ceiling
  7. fill slots under the spend policy:
     - up to max_rentals with the best candidates;
     - beyond that, up to max_rentals_burst only for high-value candidates;
     - never over the daily budget
     -> wait for HiveOS worker -> assign jetski flight sheet
  8. for rentals whose yield is well below expected on their current pool but
     whose price is still competitive: final snapshot, switch flight sheet to
     the other pool (switch cap / min window), open a new pool_run
  9. release:
     - burst rentals that no longer qualify as high value (first);
     - rentals over the premium ceiling, still underperforming after their
       pool try, or that tripped the adaptive stall detector after warm-up;
     - a rental beaten by an available candidate by the hysteresis margin
       (counting the new creation fee and boot time) when no slot is free
     final snapshot, release, delete the HiveOS worker, evaluate
     auto-blacklist rules for the released server
 10. log every decision with its inputs (scores, prices, pool yields, slot
     counts) to `decisions`; emit `events`; heartbeat for the watchdog
```

## Safety rails

- **Dry-run default**: prints what it would rent/release; `--live` required to
  spend money.
- **Rental limit**: `max_rentals` (default 2), hard ceiling
  `max_rentals_burst` for high-value rentals only.
- **Budget cap**: hard `$`/day limit in config, checked against actual charges;
  daemon refuses to exceed it, burst included.
- **Premium ceiling**: no rental above `market_price x (1 + max_premium)` per
  QUBIC; fails closed if the market price is unavailable.
- **Dead-man switch**: `watchdog.py` (separate systemd unit) watches the
  daemon's heartbeat. If the daemon stops checking in for a configurable time,
  it cancels all clorqub-managed orders (and alerts via `events`). It also
  cancels orders it doesn't recognize, if configured to. Together with the
  maximum duration on every order, this stops a dead daemon from billing.
- **Grace periods**: early boot check inside the free window; after that,
  don't judge a rig before HiveOS boot + trainer download time and enough
  shares for a measurement.
- **Hysteresis**: only replace a rig when the replacement beats it by a
  margin that covers the creation fee (spot or on-demand rate) and unpaid
  boot time.
- **Epoch awareness**: Qubic epochs roll over weekly. Don't judge a rig on data
  that spans a rollover or a trainer update (reuse the epoch-merge logic).
  Also check for Qubic idle periods (Phase 0) so they aren't read as fleet-wide
  stalls.
- **Pool-switch discipline**: min measurement window, per-rental switch cap,
  final snapshot before every switch.
- **Command validation**: the daemon validates every queued command (budget,
  rental limit, dry-run, actor allowlist); spend-increasing commands need
  confirmation.
- **Decision log**: every action + rationale persisted, so behavior is auditable.
- **Worker cleanup**: HiveOS workers are deleted when their rental ends, so the
  farm (and HiveOS per-worker billing) doesn't accumulate dead rigs.
- Secrets (`CLORE_API_KEY`, `HIVEOS_API_TOKEN`, wallet/farm hash, later the
  Discord bot token) only via env var, never in config, SQLite or logs.

## Phases

| Phase | Deliverable | Depends on |
|-------|-------------|------------|
| 0 | Verify, in this order: (1) HiveOS image + farm hash selectable through the Clore **order API** (web UI already offers it) — go/no-go; (2) creation fee rule, cancel-within-10-min behavior, max-duration option, actual-charge reporting; (3) HiveOS API v2: worker auto-registration, flight sheet assign, stats fields and reset behavior on flight sheet change, worker delete, rate limits, per-worker pricing; (4) qubjetski and qli-Client as HiveOS custom miners; (5) jetski and qubic.li per-worker stats and payout APIs; (6) QUBIC/USD and CLORE/USD price source; (7) Qubic idle periods | — |
| 1 | Read-only scanner + scorer: rank marketplace by QUBIC per dollar with premium ceiling, show effective cost per QUBIC vs market, print table. Seed perf table (it/s per CPU/GPU model) from published benchmarks and/or a few short test rentals; seed QUBIC/share from current pool payouts | 0 |
| 2 | `qubic_parse.py` (copied parsers for both miners + tests against real `stats.<epoch>.lock`/log samples) + HiveOS custom miner packages (jetski + qli with stats mirror) + SQLite schema incl. `commands`/`events` + collector + single-rental path: rent one server, early boot check, joins farm, assign flight sheet, confirm shares — on each pool, with a switch between them that loses no data | 0 |
| 3 | Share summary windows (15m/1h/2h/4h/24h/epoch; fleet / worker / server, split by pool run) + per-server history + learned perf model + payouts + blacklist (manual + auto rules, `servers`/`server <id>` reports) | 2 |
| 4 | Full loop: spend policy (2-rental limit, burst, premium ceiling, budget) + exploration + pool switching + rotation with hysteresis + watchdog | 1, 3 |
| 5 | Textual TUI client (reads SQLite, sends `commands`) | 4 |
| 6 | Later: web view (FastAPI, localhost/auth); Discord bot (posts `events`, accepts allowlisted `commands`) | 4 |

Phase 1 is usable standalone — it answers "which Clore rig mines the most
QUBIC per dollar right now, and how does that compare to buying" without
spending anything.

## Open questions to resolve in Phase 0

- Is the Clore server id stable across listings and re-listings? Does the
  marketplace expose an owner/host id, so a whole host can be blacklisted?
- Exact `clore-ai` order API (create/cancel, min durations, max duration,
  deposit rules) and how the HiveOS image gets the farm hash / rig credentials.
- Exact creation-fee rule ($0.01 spot / $0.10 on-demand): is the fee waived
  only if the order is cancelled before 10 minutes, and is the 10 minutes from
  order creation or from the server starting? (Assumed: not charged again
  when a spot rental is outbid and re-won.)
- Clore shows country and network speed (listed speed unreliable). Confirm
  both are in the API response, not just the web UI, and how a trainer
  download's progress can be read from the rig (HiveOS miner log or
  `h-stats.sh`).
- Does a new Clore HiveOS rig auto-create a HiveOS worker, and how do we map
  Clore order id to HiveOS worker id (worker name convention)?
- HiveOS custom miner: package hosting URL, and whether `h-stats.sh` can report
  shares (`ar`) in a way the API exposes. Do HiveOS stats reset on flight sheet
  change?
- HiveOS per-worker billing for short-lived rigs (prorated? minimum?).
- Does jetski expose per-worker stats and payout APIs? If not, HiveOS + SSH
  only, and QUBIC/share comes from wallet payouts.
- QUBIC/USD price source (exchange or aggregator API), and which CLORE/USD
  rate Clore uses when charging.
- Spot vs. on-demand: spot is cheaper but can be interrupted. Verify how each
  pool credits interrupted workers.
- GPU vs. CPU share yield on each pool: decides whether to rank by GPU, CPU, or
  combined.
- `qubjetski-Client` has `api.bind 127.0.0.1:17899` in appsettings. Check what it
  serves (miner was down when reviewed). It may be a cleaner stats source than
  log parsing for `h-stats.sh`.
- `qli-Client` packaging on HiveOS: config mechanism (access token /
  appsettings.json), worker naming for pool-side stats, and confirming the
  `.lock` deletion behavior also happens there.
- Does qubic.li expose a per-worker stats API suitable for yield measurement?
- Does Qubic have idle periods with no shares, and on what schedule?
