# clorqub — Qubic shares-per-cent optimizer for Clore.ai

## Goal

Maximize Qubic mining shares per dollar of Clore.ai rental spend, mining to the
**jetski pool**.

```text
score(server) = expected_shares_per_hour(hardware) / price_usd_per_hour
```

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

Move the pure, stdlib-only functions into a shared module (`qubic_parse.py`)
that both the TUI and clorqub import, instead of copying them:

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
    qubic_parse.py     # shared parsers (extracted from mining_optimizer.py)
    market.py          # Clore marketplace polling (CPU + GPU specs, price)
    perf.py            # shares/hr model keyed by (cpu model, gpu model, trainer ver)
    scorer.py          # shares-per-cent ranking
    rentals.py         # Clore order lifecycle: rent, extend, release
    hive.py            # HiveOS API: find worker, assign flight sheet, read stats
    stats.py           # measured shares: HiveOS API, pool, rig stats files
    rotate.py          # drop/replace logic (adaptive stall + score threshold)
    store.py           # SQLite: rentals, measurements, spend, decisions log
    daemon.py          # main loop
    cli.py             # status / report / dry-run / pause / set-budget
```

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
  1. snapshot marketplace (cpu/gpu specs, price/hr, availability)
  2. for each active rental: pull share stats, update perf model
  3. score all available servers
  4. rent top-scoring servers while under budget cap
     -> wait for HiveOS worker to appear -> assign jetski flight sheet
  5. release rentals whose measured score < threshold * market best, or that
     tripped the adaptive stall detector after the warm-up grace period
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
| 0.5 | Extract `qubic_parse.py` from mining_optimizer.py, keep the TUI working on it | — |
| 1 | Read-only scanner + scorer: rank marketplace by shares-per-cent, print table (seed perf table: it/s per CPU/GPU model) | 0 |
| 2 | HiveOS custom miner package + single-rental path: rent one server, it joins farm, assign flight sheet, confirm shares | 0, 0.5 |
| 3 | Stats collection + learned perf model (measured shares/hr per hardware) | 2 |
| 4 | Full loop: budget-capped auto-rent + rotation | 1, 3 |
| 5 | Optional: read-only web dashboard (FastAPI + single HTML page reading SQLite, localhost only); or a remote-rigs pane in the existing TUI | 4 |

Phase 1 is usable standalone — it answers "which Clore rig gives the most
shares per cent right now" without spending anything.

## Open questions to resolve in Phase 0

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
