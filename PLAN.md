# clorqub — Qubic shares-per-cent optimizer for Clore.ai

## Goal

Maximize Qubic mining shares per dollar of Clore.ai rental spend, mining to the
**jetski pool**.

```text
score(server) = expected_shares_per_hour(server) / price_usd_per_hour
```

`expected_shares_per_hour(server)` is that Clore server's own measured history
when there is enough of it, otherwise the hardware-model prior (see "Per-server
tracking").

Poll the marketplace, rank by score, rent the best rigs within budget, boot
them on HiveOS with a jetski flight sheet, measure actual shares, and rotate
out underperformers.

## Decisions (from user)

- **Pool: jetski** (`qubjetski-Client`, `wss://pplns.jtskxpool.ai/ws/`, PPLNS
  via `-pplns`). qli/qubic.li is no longer the default and is kept only as an
  optional secondary backend.
- **Rig OS: HiveOS image on Clore.ai.** Rented servers join the user's HiveOS
  farm. Mining is controlled through HiveOS flight sheets, not through a
  hand-rolled Docker start command.
- **Log/stats parsing: reuse `~/qubjetski-Linux-v4.5/mining_optimizer.py`**, which
  already has proven parsers (see "Reuse" below).

## Assumptions (verify in Phase 0)

- SDK: official `clore-ai` Python package (`pip install clore-ai`), auth via
  `CLORE_API_KEY` env var.
- Clore's HiveOS image can be given a farm hash / rig id at order time, so a
  new rental auto-registers as a HiveOS worker.
- HiveOS API v2 (token auth) can: list workers, assign a flight sheet
  (`PATCH /farms/{farm}/workers/{worker}` `{"flight_sheet_id": X}`), and
  return per-worker miner stats.
- qubjetski runs on HiveOS as a **custom miner** package, since it isn't a
  built-in HiveOS miner.
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
- `TRAINER_UPDATED_RE`: trainer variant + version (`v<epoch>.<build>`), so the
  version is included in the perf model and an update doesn't look like a
  performance change.
- `parse_stats_file`, `find_stats_files`, `epoch_history`: `stats.<epoch>.lock`
  JSON (`FoundShares`, `SubmittedShares`, `InvalidShares`,
  `Solutions[].Created/ReportedScore/Invalid`). Authoritative share counts.
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

Python daemon + CLI, SQLite for state. No web UI in v1.

```text
clorqub/
  config.toml          # budget cap, intervals, farm/flight-sheet ids, thresholds
  hive/qubjetski/      # HiveOS custom miner package (tar.gz served to rigs)
    h-manifest.conf    #   name, version, log path
    h-config.sh        #   render appsettings.json from flight-sheet vars
    h-run.sh           #   exec qubjetski-Client -start -pplns
    h-stats.sh         #   parse log/stats -> khs, ar[acc,rej], uptime JSON
  clorqub/
    qubic_parse.py     # parsers copied from mining_optimizer.py (original untouched)
    market.py          # Clore marketplace polling (CPU + GPU specs, price)
    perf.py            # shares/hr: per-server history blended with hardware prior
                       #   (cpu model, gpu model, trainer ver)
    scorer.py          # shares-per-cent ranking
    rentals.py         # Clore order lifecycle: rent, extend, release
    hive.py            # HiveOS API: find worker, assign flight sheet, read stats
    stats.py           # measured shares: HiveOS API, pool, rig stats files
    rotate.py          # drop/replace logic (adaptive stall + score threshold)
    store.py           # SQLite: servers, rentals, measurements, blacklist,
                       #   spend, decisions log
    daemon.py          # main loop
    cli.py             # status (share windows) / report / dry-run / pause / set-budget /
                       #   servers / server <id> / blacklist add|rm|list
```

### Per-server tracking and blacklist

Every rental is keyed by its **Clore server id**, so we can see how each
physical server does over time, across rentals.

SQLite tables:

- `servers`: clore `server_id` (plus host/owner id if Clore exposes one),
  hardware specs as listed, first/last seen, price history.
- `rentals`: clore order id, `server_id`, HiveOS worker id, start/end, price,
  spot/on-demand, end reason (rotated, stalled, interrupted, budget, manual).
- `measurements`: `rental_id`, `server_id`, timestamp, epoch, trainer version,
  it/s, found/submitted/invalid shares (cumulative per epoch, as in
  `stats.<epoch>.lock`).
- `shares`: individual share events (`worker`, `server_id`, `rental_id`, `Created` UTC,
  epoch, invalid, `ReportedScore`), deduped by `SolutionHash`. These come from
  `stats.<epoch>.lock` `Solutions[]`. When only counters are available
  (HiveOS/pool), counter deltas between polls are stored as timestamped events.
- `blacklist`: `server_id` (or host id), reason, source (auto/manual), added
  at, optional expiry.

Use:

- **Scoring**: blacklisted servers are dropped before ranking. For other
  servers, blend that server's measured shares/hr with the hardware prior,
  weighted by how much rented time we have on it. A known-good server can then
  beat an untested one with the same specs, and the reverse.
- **Auto-blacklist** (thresholds in config, every trigger logged with its data):
  - measured shares/hr < X% of the hardware prior over >= N hours of rentals;
  - never joined HiveOS or never produced a share within the boot grace period;
  - repeated stalls (the adaptive stall detector) or spot interruptions;
  - invalid/rejected share ratio above a threshold;
  - listed specs don't match what the rig reports (CPU/GPU model, thread count).
  Auto entries expire after a configurable period so a fixed server can come
  back. Manual entries never expire.
- **Reports**: `cli.py server <id>` gives a server's rental history, shares/hr
  over time, cost per share and incidents. `cli.py servers` ranks every server
  we've rented by lifetime shares-per-cent.

### Share summary windows

Share counts for **15m / 1h / 2h / 4h / 24h / epoch** (windows in config
`summary_windows`), shown at these levels:

- **fleet**: all active rentals, plus total $/h and shares per cent per window;
- **per worker**: one row per HiveOS worker / jetski worker name (`alias`,
  `-workername`), with rental id, `server_id`, hardware, uptime and $/h
  alongside the windows. Also includes workers clorqub didn't rent (e.g. your
  local rig) if HiveOS or the pool reports them, marked as unmanaged;
- **per rental / server**: windows for one rental, or rolled up across all of a
  server's rentals;
- **per server history**: the same windows in `cli.py server <id>`.

Each window shows found / invalid shares, shares/h, and shares per cent (spend
over the same window). Computed from the `shares` table with the
`count_shares_in_window` logic and the epoch-rollover merge. "Epoch" is the
current Qubic epoch only. A window longer than a rental's uptime is marked
partial (e.g. `12 (3h)`), so a new rig isn't compared unfairly.

Shown by `cli.py status` (fleet + per-worker table; `--by server|rental`) and in the optional Phase 5
dashboard.

### Share data sources (in order of preference)

1. **HiveOS API worker stats**, filled in by our `h-stats.sh` (hashrate +
   accepted/rejected shares). Centralized, no SSH. Poll slowly (rate limits).
2. **Jetski pool API** (per-worker shares by `alias`/workername), if one exists.
   Independent of the rig, so it catches rigs that are lying or offline.
3. **`stats.<epoch>.lock` over SSH** via HiveOS/Clore SSH access: authoritative,
   used for audit and for calibrating the perf model.

### Core loop

```text
every N minutes:
  1. snapshot marketplace (server_id, cpu/gpu specs, price/hr, availability);
     drop blacklisted servers
  2. for each active rental: pull share stats, update perf model
  3. score all available servers
  4. rent top-scoring servers while under budget cap
     -> wait for HiveOS worker to appear -> assign jetski flight sheet
  5. release rentals whose measured score < threshold * market best, or that
     tripped the adaptive stall detector after the warm-up grace period;
     evaluate auto-blacklist rules for the released server
  6. log every decision with its inputs (scores, prices) to SQLite
```

## Safety rails

- **Dry-run default**: prints what it would rent/release; `--live` required to
  spend money.
- **Budget cap**: hard `$`/day limit in config; daemon refuses to exceed it.
- **Max concurrent rentals** cap.
- **Grace period**: don't release a rig before Clore's minimum billing period,
  HiveOS boot + trainer download time, and enough shares for a measurement.
- **Epoch awareness**: Qubic epochs roll over weekly. Don't judge a rig on data
  that spans a rollover or a trainer update (reuse the epoch-merge logic).
- **Decision log**: every action + rationale persisted, so behavior is auditable.
- Secrets (`CLORE_API_KEY`, `HIVEOS_API_TOKEN`, wallet/farm hash) only via env
  var, never in config or logs.

## Phases

| Phase | Deliverable | Depends on |
|-------|-------------|------------|
| 0 | Verify: clore-ai order API + HiveOS-image params; HiveOS API v2 (worker auto-registration, flight sheet assign, stats fields, rate limits); qubjetski as HiveOS custom miner; jetski pool API | — |
| 1 | Read-only scanner + scorer: rank marketplace by shares-per-cent, print table (seed perf table: it/s per CPU/GPU model) | 0 |
| 2 | `qubic_parse.py` (copied parsers + tests against real `stats.<epoch>.lock`/log samples) + HiveOS custom miner package + single-rental path: rent one server, it joins farm, assign flight sheet, confirm shares | 0 |
| 3 | Stats collection + share summary windows (15m/1h/2h/4h/24h/epoch; fleet / worker / server) + per-server history + learned perf model + blacklist (manual + auto rules, `servers`/`server <id>` reports) | 2 |
| 4 | Full loop: budget-capped auto-rent + rotation | 1, 3 |
| 5 | Optional: read-only web dashboard (FastAPI + single HTML page reading SQLite, localhost only); or a remote-rigs pane in the existing TUI | 4 |

Phase 1 is usable standalone — it answers "which Clore rig gives the most
shares per cent right now" without spending anything.

## Open questions to resolve in Phase 0

- Is the Clore server id stable across listings and re-listings? Does the
  marketplace expose an owner/host id, so a whole host can be blacklisted?

- Exact `clore-ai` order API (create/cancel/extend, min durations, deposit
  rules) and how the HiveOS image gets the farm hash / rig credentials.
- Does a new Clore HiveOS rig auto-create a HiveOS worker, and how do we map
  Clore order id to HiveOS worker id (worker name convention)?
- HiveOS custom miner: package hosting URL, and whether `h-stats.sh` can report
  shares (`ar`) in a way the API exposes.
- Does jetski expose a per-worker stats API? If not, HiveOS + SSH only.
- Spot vs. on-demand: spot is cheaper but can be interrupted. With PPLNS the
  share window matters less than for epoch-total scoring, but verify how jetski
  credits interrupted workers.
- GPU vs. CPU share yield on jetski: decides whether to rank by GPU, CPU, or
  combined.
- `qubjetski-Client` has `api.bind 127.0.0.1:17899` in appsettings. Check what it
  serves (miner was down when reviewed). It may be a cleaner stats source than
  log parsing for `h-stats.sh`.
