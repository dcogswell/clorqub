# clorqub — Qubic accumulation optimizer for Clore.ai

## Goal

**Accumulate as much QUBIC as possible per dollar** of Clore.ai rental spend,
mining to the **jetski pool** with **qli (qubic.li)** as a switchable secondary
pool. A slight loss versus the QUBIC market price is acceptable (QUBIC is near
its all-time low, so the aim is to stack coins, not to earn USD). Spend is
held back by a small rental limit plus daily and per-epoch budgets, not by a
profit gate.

```text
score(server, pool) = expected_qubic_per_hour(server, pool) / cost_usd_per_hour(server)
                    = QUBIC per dollar

expected_qubic_per_hour = sols_per_hour(server, pool)       # per-rig, measured
                        x qubic_per_solution(pool, epoch)    # from pool payouts

effective_cost_per_qubic = 1 / score                         # USD per QUBIC mined
```

- `sols_per_hour(server, pool)`: use whole-rig solutions/hour from a pool or
  worker view when available. Keep CPU trainer (AVX2/AVX512), GPU trainer
  (CUDA), and package/trainer version distinct. Raw `it/s` from different
  trainers is not comparable. Until the payout conversion is known, the
  read-only scanner ranks same-pool candidates by measured/anchored sols/hour
  per dollar; that is an ordering proxy, not a QUBIC payout estimate.
- `qubic_per_solution(pool, epoch)`: derive from actual payouts and pool-side
  solution/score accounting. qli pays per share (PPS).
  jetski has solo and PPLNS endpoints (currently the same server per their
  Discord); start with a rough estimate and refine it from payouts.
- `cost_usd_per_hour`: Clore price (API gives USD **per day**; divide by 24)
  + amortized creation fee (see "Clore billing").

Ranking is in QUBIC per dollar, so it doesn't depend on the QUBIC/USD price.
The market price is only used for the premium ceiling and the high-value test
below.

### CPU/GPU yield model and initial benchmark anchors

QUBIC has separate CPU trainers for AVX2 and AVX512, plus a CUDA GPU trainer.
The live jetski "ALL POOLS" figures (CUDA 31,060, AVX512 17,235, AVX2 1,705
sol/hr in the observed view) are pool-wide totals, not rig benchmarks; they
must not be used as per-server yield. Likewise, `it/s` from CPU and GPU logs
has trainer-specific scales and is not directly additive/comparable.

2026-11-15 observation: the dashboard showed ~50,000 sol/hr pool-wide, split
AVX512 27,257 / AVX2 19,669 / ~3,074 third (likely CUDA), and the user reports
the total sits near 50k ~99% of the time. Two snapshots days apart both total
~50k but with a very different trainer split (CUDA-dominant earlier,
AVX512-dominant now) — consistent with the algorithm changing per epoch.
Implications, held as hypotheses until same-tree data confirms them:

- Pool output looks like a **fixed-rate pie**: individual dry spells are
  redistributive (the solutions went elsewhere), not pool-wide droughts.
- Long-run rig yield ≈ share of pool work × ~50k sol/hr, so **it/s per USD
  is the right ranking metric** and whole-rental shares/$ is meaningful
  once several tree transitions are included — short windows still aren't.
- Re-rolling a dry tree is a **variance trade** (a fresh lottery draw), not
  expected gain, unless some assignments turn out persistently bad.
- The trainer split shifting between observations means CPU-vs-GPU yield
  comparisons must be **per-epoch**: a GPU-vs-CPU conclusion from one
  algorithm may not carry to the next.

The user's initial whole-rig jetski observations seed conservative priors:

| GPU | CPU reference | Whole-rig yield prior |
|---|---|---:|
| RTX 3070 | Ryzen 9 7945HX or 7950X (AVX512-capable) | about 160 sols/hr |
| RTX 3080 Ti | same class | >350 sols/hr (score at a 350 floor) |
| RTX 3090 Ti | same class | >350 sols/hr (score at a 350 floor) |

These numbers include the CPU contribution; they are **not GPU-only**. The
offered CPU thread count for the reference runs was not provided, so preserve
that as unknown and do not pretend the priors are per-thread. A 3070 at about
160 is an approximate point estimate; the Ti observations are lower bounds.
The first scanner only applies these priors to a single-GPU listing with a
matching model and a listed Ryzen 9 7945HX or 7950X CPU. It does not
extrapolate even to other AVX512 CPUs (including 7950X3D/9950X), AVX2,
other GPU models, laptop GPUs, or multi-GPU hosts. Because reference CPU
allocation is unknown, these are provisional shortlist scores, not
thread-normalized forecasts or guaranteed yield floors for a new listing.
Unmatched listings remain visible in SQLite but have no fabricated yield
score.

Additional user-reported context is stored independently from scoring:
about **35 CPU it/s**, about **230 GPU it/s** with no exact GPU/run identified,
and a tentative **100 GPU it/s for RTX 3070** ("I think"). Do not assign the
230 figure to both Ti models or infer CPU/GPU solution contributions from
these observations. Pool, epoch, trainer version, measurement duration and
thread allocation must be recorded alongside future measurements; unknown
metadata stays unknown for these historical recollections.

CPU ISA detection from the marketplace CPU name is only a pre-rental hint.
On the rental, read actual CPU flags and require `avx512f` before assigning an
AVX512 measurement; otherwise downgrade the profile or fail the expected
hardware check. Keep AVX2 and AVX512 CPU measurements separate, along with
CUDA measurements, package/trainer version, pool, and allocated thread count.
Learned whole-rig yields should be based on sustained pool/worker solutions
per hour. Keep an active-mining rate for hardware calibration, but use
wall-clock yield (including billable warm-up, idle and downtime) for economic
ranking, or explicitly apply a measured duty-cycle adjustment. Pool-wide
totals are not per-rig calibration; a short lucky share window does not
establish a reliable prior.

Initial scanner implementation: `python3 cli.py scan` is read-only. A saved
marketplace response can be ranked with `python3 cli.py scan --snapshot
marketplace.json --db path/to/clorqub.sqlite3`; live mode reads
`CLORE_API_KEY` from the environment or `~/.config/clorqub/env`. The SQLite
database stores the latest raw marketplace response (older scans keep only
time/source/server count), change-only server/spec/price history, and
these sourced benchmark anchors plus raw it/s context. An offline file's
import timestamp is not its original observation time or fresh availability.
The initial ranking uses on-demand pricing
plus an assumed 5% renter fee from the earlier API observation (not a fresh
fee lookup), excludes the creation fee and boot opportunity cost,
and filters RU/UA/BY plus CUDA listings below 12.0. Spot ranking is intentionally
not shown until the scanner fetches and converts each server's live spot
minimum; the marketplace USD spot field is a zero placeholder, not a free
quote. This stage never creates or cancels orders.

### Spend policy

- **Premium ceiling:** don't rent a server whose `effective_cost_per_qubic` is
  above `market_price x (1 + max_premium)`. `max_premium` is in config
  (default 0.25 = pay up to 25% more per QUBIC than buying it). This allows a
  slight loss and rules out rigs that are far worse than just buying QUBIC.
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
- **Epoch budget cap:** configurable `epoch_budget_usd` limits total managed
  rental spend for one QUBIC epoch. It is independent of the daily cap;
  both must pass before new spending is allowed. No dollar amount has been
  selected yet.
- Market price comes from an exchange/aggregator API, cached; if it is
  unavailable, the ceiling and burst tests fail closed (no new rentals beyond
  `max_rentals`, no burst).

### Epoch budget

- Covers all clorqub-managed rentals, normal and burst, on both pools, plus
  discovered manual orders unless explicitly reviewed and excluded.
  Count rental charges, renter fees and creation fees, retaining native
  amounts and the USD conversion used for each charge.
- Key the budget and accounting by the confirmed QUBIC epoch id and UTC
  boundaries, not a local calendar week. Persist them in SQLite so daemon
  restarts, miner restarts and pool switches never reset the spent amount.
  If the epoch boundary is uncertain, do not reset the budget or admit
  spending that depends on a reset.
- Track **cap / actual spent / unbilled reserve / remaining** separately.
  Remaining = cap - actual spent - reserve; the reserve covers accrued
  charges not yet reported, pending fees, and estimated cost until the next
  control check plus cancellation delay. Reconciliation replaces reserves
  with actual charges rather than counting both.
- Reject new rentals, bursts and rebids that cannot fit inside the remaining
  allowance. Release managed rentals before forecast spend crosses the cap,
  taking a final snapshot where reachable. Cancel paused spot orders too,
  so they cannot resume and unexpectedly spend the exhausted budget.
- Rentals spanning a boundary have time-based spend attributed to the epoch
  in which it accrued; one-time fees belong to the epoch in which charged.
  Do not charge the full lifetime rental total to each epoch. Unused
  allowance does not carry over by default. A configured recurring cap may
  apply to each new confirmed epoch; an explicit epoch override takes
  precedence. A budget is a ceiling, not an instruction to spend it all.
- CLI/TUI expose the current epoch, cap, actual spend, reserve, remaining
  allowance, fleet burn rate and approximate time to budget exhaustion.
  Set/change it through an audited `set-epoch-budget` command using the same
  daemon-side validation and spend-increase confirmation as daily budgets.
  Lowering the cap below already-spent/reserved funds blocks further spend;
  it cannot undo existing charges.
- This is a planned daemon spending control, not implemented by the current
  read-only scanner or TUI.

### Manual epoch earnings by pool

- Allow the user to enter **QUBIC earned for a specific epoch and pool**
  through the CLI/TUI, even when no usable pool payout API exists. Keep
  jetski and qli separate, including payout mode/account scope where known.
  No earnings amount is assumed.
- Record epoch id, pool, QUBIC amount, covered time window, scope
  (managed rentals / selected workers / whole pool account), source
  `manual`, entry time and optional reference/note. Explicit zero earnings
  is valid; missing earnings are unknown, not zero.
- Distinguish estimated/accrued earnings from finalized/confirmed earnings,
  and earnings from an actual payout transfer. An accrued total followed by
  a final total or payout must not be counted as three earnings events.
  Corrections supersede the previous entry for that scope/window, with an
  audit trail, rather than adding a second copy.
- Feed compatible observations into the pool/epoch yield model:
  QUBIC per accepted solution/share or score unit, matched to the same
  accounting window and payout definition. Use this to refine expected
  QUBIC/hour, pool selection, effective USD/QUBIC and rental value.
  Never treat jetski and qli share/score units as interchangeable.
- If a pool account also includes the user's own rigs or unrelated workers,
  do not attribute the entire entered total to rentals. Require matching
  worker attribution or sufficiently complete account-wide work data before
  deriving rental-specific yield. Otherwise retain the entry for reporting
  and mark calibration unavailable/low-confidence.
- Estimated or sparse entries inform uncertain priors, not immediate pool
  switching. Preserve minimum measurement windows, switch cooldowns and
  trainer/epoch boundaries. API imports must reconcile with manual entries
  so the same earnings are not counted twice.
- Show earnings per epoch/pool and which entries calibrated the model.
  Gross mining earnings do not reduce actual rental spend or automatically
  replenish the daily/epoch spending caps. This input path is planned, not
  implemented in the current read-only viewer.

### Manual rentals and startup reconciliation

- The user may create, rebid, pause/resume or cancel rentals directly in
  Clore's web UI. On startup, fetch `GET my_orders` before any automated
  rental admission; periodically reconcile on the normal five-minute scan
  cadence too. The local database is not the sole authority for order
  existence.
- Match orders by Clore order id, retaining server id, native currency,
  quoted price, reported spend and observed state. Import unknown orders
  as **discovered / observe-only**, never silently creating a duplicate
  rental or assuming an alias proves clorqub ownership.
- Show manual orders in the TUI even if their hardware is absent from the
  current available marketplace. An inventory failure means unknown/stale,
  not zero orders; retain the last successful inventory and block future
  automated spending until reconciliation is trustworthy.
- Discovery is not adoption: do not SSH/bootstrap, change miners or pools,
  rebid, cancel, or otherwise control an external order until the user
  explicitly adopts it through an audited confirmation. Adoption reuses the
  existing order/history rather than resetting spend or creating a new order.
- Conservatively include discovered orders in rental-slot and daily/epoch
  budget admission checks unless explicitly excluded. The automation cannot
  prevent the user from overspending manually: alert and block its own new
  spending, rather than cancelling unadopted orders.
- Reconcile changes made in the web UI. An order missing from one successful
  active-order response is "not returned", not proven cancelled; confirm
  completion/charges before closing the ledger. Keep paused orders visible
  because they may resume and bill again.
- A pre-existing order's cumulative `spend` is not automatically current
  epoch spend, nor proof all fees are included. Attribute charges using
  billing timestamps/history; mark unknown allocation conservatively.
  Store a whitelist of non-secret order metadata, never returned passwords,
  tokens, order environment variables, SSH material or startup commands.

Poll the marketplace, rank by QUBIC per dollar, rent the best rigs within the
spend policy, boot them with the clorqub miner image, measure, switch pools on
rigs that aren't yielding, and rotate out underperformers.

## Decisions (from user)

- **Objective: accumulate QUBIC.** Rank by QUBIC per dollar; slight loss vs
  market price is OK; spend limited by rental count plus daily and epoch
  budgets (see "Spend policy").
- **Rental limit: 2 concurrent**, extendable only for high-value rentals.
- **Pools: jetski (primary) + qli/qubic.li (secondary), switchable per rig on
  yield.** jetski: `qubjetski-Client`, `wss://pplns.jtskxpool.ai/ws/`. qli:
  `qli-Client` against the qubic.li pool, always PPS. The agent downloads both
  miner packages at container start (see "Rig runtime"), so switching pools
  is a restart of the miner inside the running rental — the rental stays up. A cheap rental that isn't
  yielding on one pool gets tried on the other before we release it. Payout
  mode (jetski solo vs PPLNS) is not a design driver for now.
- **Rig runtime: Clore's HiveOS Docker image, without running HiveOS.** The
  user is on the HiveOS free plan, which has no API access (Hiveon: "API
  Access will now be limited to paid farms only"). Rentals use
  `cloreai/hiveos:0.4` (fast to start on Clore, likely cached on hosts) with
  the order's `command` replaced by a clorqub bootstrap that fetches our
  agent; the daemon controls the agent over SSH. No image of our own to
  build or host. Miners are **not baked in**: like HiveOS, the agent
  downloads the pools' HiveOS custom-miner packages from their `latest`
  release URLs on the fly, because both projects release often and
  third-party Docker images (e.g. qli's) fall behind. Benefits: no HiveOS
  API, worker cap or bill; stats come straight from the miners' own files;
  boot, pull and download times are measured directly. Lost: HiveOS
  dashboards and GPU overclocking (not needed for v1).
- **Log/stats parsing: reuse `~/qubjetski-Linux-v4.5/mining_optimizer.py`**,
  which already has proven parsers for both miners (see "Reuse" below).
- **Hardware specs kept in SQLite for reference**: every server's listed CPU
  and GPU details, a history of changes, and what the rig actually reported
  (see "Hardware reference").
- **UI: headless daemon + separate clients.** The daemon runs unattended under
  systemd. CLI, a Textual TUI, a later web view and a future Discord bot are
  all separate processes that read SQLite and send commands through it (see
  "Process model").
- **Secrets:** `~/.config/clorqub/env` (mode 600), loaded as environment
  variables; the systemd units use `EnvironmentFile=`. Holds
  `CLORE_API_KEY`, `QLI_ACCESS_TOKEN` (qli pool needs an access token) and
  `JETSKI_WALLET` (jetski needs a wallet id; a public address, but kept in
  the same file so all credentials live in one place).

## Phase 0 findings (2026-11-14)

Verified against the live Clore API (read-only calls) and docs.

**Clore API**

- Base `https://api.clore.ai/v1`, header `auth: <key>` (not Bearer).
  Responses carry `code` (0 ok, 5 = rate limited).
- Rate limits: ~1 request/s generally; `create_order` 1 per 5 s;
  `set_spot_price` price reductions once per 600 s.
- Endpoints in use: `GET marketplace`, `GET spot_marketplace?market=<id>`,
  `GET my_orders[?return_completed=true]`, `GET wallets`,
  `POST create_order`, `POST cancel_order {"id"}`, `POST set_spot_price
  {"order_id","desired_price"}`.
- `create_order` fields: `renting_server`, `type` (`on-demand` / `spot`),
  `currency` (`CLORE-Blockchain`, `bitcoin`, `USD-Blockchain`), `image`,
  `ports` (`{"22":"tcp"}`), `env`, `command`, `ssh_key` / `ssh_password`,
  spot bid price. HiveOS is a Clore CCR image id (`c9a4e2f6b7d488d8f0bab0ff`),
  no longer needed.
- Marketplace snapshot: 2848 servers, 1645 available. **No CPU-only
  listings**; every server has at least one GPU. CPU and GPU are sold as a
  bundle, and CPU/AVX512 yield can materially change the bundle's value.

**Marketplace fields (cross-checked against the web UI)**

| API field | Meaning |
|---|---|
| `id` | server id |
| `owner` | **hoster id** (web UI "hoster: 55772") — enables host-level blacklist |
| `price.usd.on_demand_usd` | **USD per day** (web $0.18/h = API 4.1/day); web is ~2–7% higher than API/24 (fee or rounding — check against actual charges) |
| `price.on_demand.<currency>`, `price.spot.<currency>` | per-currency prices; `price.usd.spot` is always 0 |
| `mrl` | max rental **hours** (72 = "3d") |
| `reliability` | fraction 0–1 (0.893 = 89.3%) |
| `rating.avg/cnt` | host rating |
| `specs.cpu` | CPU model string (can be "Unknown CPU") |
| `specs.cpus` | **cores/threads offered to the renter**, e.g. `8/8` on a 9950X3D — use this, not the CPU model, for CPU capacity |
| `specs.ram`, `disk`, `disk_speed`, `mb`, `pcie_rev`, `pcie_width`, `pl`, `stock_pl` | RAM GB, disk, MB/s, motherboard, PCIe, power limits |
| `gpu_array`, `specs.gpu`, `specs.gpuram`, `cuda_version` | GPU models (one per card), VRAM, CUDA |
| `specs.net.cc` | **country code** |
| `specs.net.down/up`, `test_history` (`S\|unix_ts\|down\|up`) | listed speed + test history |
| `partial_gpu_rental` | `false`, or `{total_gpus, available_gpus, free_indices}` |

- **Listed network speed is stale:** median latest speed test is **52 days**
  old (p90: 385 days). Confirms the user's experience; measured download time
  is the real signal.
- **Region:** RU 499 + UA 139 = **39%** of available servers. Blocking them
  removes a lot of supply but is the user's call (bandwidth). BY (57) is next.
- Price spread (on-demand, USD/day): p10 2.0, median 11, p90 40. Per-GPU
  median: RTX 3090 $4.50/day, 4090 $10.94/day, 5090 $15/day.
- **Spot:** `spot_marketplace` returns the server's `min_pricing` per
  currency, current `offers` (competing bids), `online`, and
  `currency_rates_in_usd` (CLORE was $0.00149, BTC $66.5k) — a usable
  CLORE/USD source for spend accounting.

**HiveOS** — dropped (free plan has no API; see Decisions).

**Miner packages** (HiveOS custom-miner tarballs, downloaded and inspected):

| Pool | URL (`latest`) | Size | Version | Binary |
|---|---|---|---|---|
| qli | `https://github.com/qubic-li/hiveos/releases/download/latest/qubminer-latest.tar.gz` | 8.1 MB | 3.13.6 | `qubminer/qli-Client` |
| jetski solo | `https://github.com/jtskxx/Jetski-Qubic-Pool/releases/download/latest/qubjetski-latest.tar.gz` | 6.5 MB | 4.5 | `qubjetski/qubjetski-Client` |
| jetski PPLNS | `https://github.com/jtskxx/Jetski-Qubic-Pool/releases/download/latest/qubjetski.PPLNS-latest.tar.gz` | 6.5 MB | 4.5 | `qubjetski.PPLNS/qubjetski-Client` |

- Each tarball has a sibling **`.hash`** (same URL, `.hash` instead of
  `.tar.gz`) holding its SHA-256; all three matched the downloads. The
  agent verifies every download against it, and uses it to detect a new
  release.
- Downloads took ~2 s each from here, so on a good host the miner is ready
  within seconds; slow hosts show up directly in the boot timeline.
- **The two jetski packages ship the identical binary.** They differ only in
  `appsettings_global.json`: solo = `wss://solo.jtskxpool.ai/ws`,
  `"pps": false`; PPLNS = `wss://pplns.jtskxpool.ai/ws`, `"pps": true` (the
  local rig uses the PPLNS settings). So jetski solo vs PPLNS is a config
  choice, not a separate miner.
- jetski config (`appsettings.json`): `pool.url/wallet/alias/pps`,
  `miner.cpu.enabled/version/threads`, `miner.gpu.enabled/version/cards`,
  `api.bind 127.0.0.1:17899`. Its `h-stats.sh` reads
  **`http://127.0.0.1:17899/summary`** (JSON: mode, epoch, cpu/gpu runner
  versions, per-GPU stats), so the agent can use that instead of log
  parsing for jetski.
- qli config (`appsettings.json` under `ClientSettings`): `poolAddress`
  (`wss://wps.qubic.li/ws`), `alias`, `accessToken` or `qubicAddress`,
  `trainer.cpuThreads`, `trainer.cpu/gpu`, optional `Idling` commands.
  Stats come from the log (`h-stats.sh` parses it). qli-Client downloads its
  trainer runners at runtime.
- Packages' `h-run.sh` need `ts` (moreutils) and jetski's `h-config.sh`
  self-updates via the hash; both assume HiveOS paths (`/hive/...`). The
  agent doesn't run the `h-*` scripts: it extracts the binary, writes
  `appsettings.json` itself, and does its own timestamping and updates.
- qli's config supports `Idling` pre/idle/post commands, so Qubic does have
  idle phases; the schedule is still to be confirmed.
- **Both binaries run outside HiveOS** in a plain `ubuntu:24.04` container
  (tested with no network and no credentials). `qubjetski-Client` is a
  static Go binary; `qli-Client` needs only glibc + libstdc++ (present in
  the base). jetski also has setup flags (`-wallet`, `-workername`, `-cpu`,
  `-threads`, `-gpu`, `-gpu-version`, `-gpu-cards`, `-pplns`), but `-start`
  reads `appsettings.json`, so the agent writes that file. GPU runtime needs
  are untested (no GPU here).

**Test rental (2026-11-14, server 120113: Moldova, RTX 3060, Ryzen 9 3900
5/10, spot $0.95/day USD-Blockchain, `cloreai/hiveos:0.4` + bootstrap at
`ab5b84c5`):**

- **Startup:** order 15:53:52 → bootstrap running 15:54:02 (**10 s**; the
  image was cached on the host) → agent files hash-verified 15:54:04 →
  credentials pushed over SSH 15:54:56 → jetski package verified in 2.8 s
  → miner 15:55:00 → **first it/s 15:55:20**.
- **Order rules found:**
  - `env` must **not** set `NVIDIA_VISIBLE_DEVICES` /
    `NVIDIA_DRIVER_CAPABILITIES`: such orders are refused with a bare
    `{"code":1}` (HTTP 500). Clore and the image set these anyway.
  - With a custom `command`, Clore's `ssh_key` field doesn't install the
    key (SSH refused), so the bootstrap installs it from
    `CLORQUB_SSH_PUBKEY`.
  - Orders return `pub_cluster` (e.g. `n1.de.clorecloud.net`) and
    `tcp_ports` (`"22:2009"` = container 22 → proxy port 2009).
  - `my_orders` also shows the user's earlier manual HiveOS rentals with
    `image: cloreai/hiveos:0.4`, so the web UI's HiveOS option is this
    image.
- **Billing (actual):** `spend` = seconds running × price/day ÷ 86400 ×
  1.0125 (spot renter fee). A test order cancelled after 75 s cost $0.0008
  (no creation fee → **cancel inside 10 min is free** except time). The
  main order cost $0.0094 for 841 s. Paused time isn't billed.
- **GPU works on the HiveOS image:** RTX 3060 at 100% util, ~100 W, host
  driver 620.70. The GPU trainers have no CUDA library dependencies, so
  the image's baked CUDA 11.7 doesn't matter. No CUDA workaround needed.
- **Hashrate:** jetski GPU **84–85 it/s**; jetski CPU (9 threads AVX2)
  **1–4 it/s**, so the GPU produces nearly all the it/s on a rig like this.
  qli GPU **41 it/s 2 min after start, still rising** (order paused before
  it settled; qli CPU not measured).
- **Pool switch:** jetski → qli in **3 s** (package prefetched in the
  background).
- **Spot outbid:** after ~14 min another bid took the server. The order
  became **`paused: true`** (`offers[].active: false`), the container
  stopped, SSH went away, billing stopped. The daemon must treat `paused`
  as an interruption (rebid, wait, or cancel and replace), and the agent
  state (`state.json`) must survive the resume.
- **Log format:** jetski logs one line with both rates
  (`[AVX2] 1 it/s | [CUDA] 84 it/s | 78 avg it/s`); qli logs separate
  `[AVX2]` and `[CUDA]` lines. The agent v1 parser read only the first rate
  (status showed `it: 1`); fix to record per-device rates.
- **Pool dashboard:** the worker showed up on the jetski dashboard under
  its alias, so the wallet/alias delivered over SSH work.

**Still open from Phase 0:** see "Open questions".

## Reuse from mining_optimizer.py

`mining_optimizer.py` is **not modified**. The pure, stdlib-only functions
clorqub needs are copied into `clorqub/qubic_parse.py`, with a header comment
naming the source file so later fixes can be ported by hand. The in-container
agent uses the same module.

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
  in the local `/var/qli.error.log`). The agent copies changed `.lock` files
  to a safe dir, and all reads use the mirror.
- `count_shares_in_window`, the two-newest-epoch merge (so an epoch rollover
  doesn't drop shares or look like a stall), and the within-run inter-share gap
  median (including the `times[-65:]` slicing fix).
- `tail_log_lines`, `read_new_log_lines`, `ANSI_RE`: incremental log tailing.
- Adaptive stall logic (`stall_gap_multiplier` x median gap, clamped): reused as
  the "this rental is dead / underperforming" detector in `rotate.py`.

Not reused: the curses TUI, `ServiceManager`/sudo/systemctl handling, the
thread-mode switching, and the port-31111 dual-miner workaround (one miner
runs at a time in a rental).

## Architecture

Python. Headless daemon + clients, SQLite (WAL mode) for state.

```text
clorqub/
  config.toml          # max_rentals / burst settings, max_premium, daily/epoch caps,
                       #   intervals, country_blocklist, thresholds, rig image,
                       #   bootstrap (pinned agent commit + hashes)
  rig/                 # runs inside rentals (fetched by the bootstrap)
    bootstrap.sh       #   order `command`: fetch + verify agent, lock down
                       #   image defaults, start sshd key-only, exec agent
    agent.py           #   in-container supervisor: fetch + verify packages,
                       #   start/stop/switch miner, update on new release,
                       #   write status.json, mirror .lock files, boot timeline
    render_config.py   #   appsettings.json for jetski (solo/pplns) / qli from env
  clorqub/
    qubic_parse.py     # parsers copied from mining_optimizer.py (original untouched)
    market.py          # Clore marketplace polling (specs, country, price)
    hardware.py        # spec normalization (CPU/GPU model names) + reference tables
    price.py           # QUBIC/USD and CLORE/USD market price (cached)
    perf.py            # it/s per server (history blended with hardware prior:
                       #   cpu model + threads offered, gpu model, trainer ver);
                       #   per-pool shares/iteration and QUBIC/share
    scorer.py          # QUBIC-per-dollar ranking + spend policy (ceiling,
                       #   rental limit, burst)
    clore.py           # Clore API client (auth header, rate limits, retries)
    rentals.py         # order lifecycle: rent, early check, release
    rig.py             # SSH to a rental's container: agent status, switch
                       #   pool, fetch .lock files, final snapshot
    collector.py       # poll rigs + pool APIs -> local SQLite snapshots
    stats.py           # derive share events, windows and rates from snapshots
    rotate.py          # drop/replace + pool-switch logic (adaptive stall,
                       #   yield vs expected, hysteresis, burst unwind)
    store.py           # SQLite schema + access; the only writer is the daemon
    daemon.py          # main loop; executes queued commands
    watchdog.py        # dead-man switch (separate process)
    cli.py             # status (share windows) / report / dry-run / pause /
                       #   set-budget / set-epoch-budget / set-max-rentals / servers /
                       #   server <id> / hardware / blacklist add|rm|list /
                       #   switch-pool / release
    tui.py             # Textual viewer (Phase 5)
```

Current layout (Phase 1, flat at the repo root; the package split above is
the target): `cli.py` (scan CLI), `market.py` (marketplace ingest, change
keys, scorer), `spot.py` (bounded spot-floor observations), `orders.py`
(`my_orders`/wallet inventory), `scanner_watch.py` + `collector.py` +
`collector_service.py` (opt-in transient systemd `--user` scanner),
`rent.py` + `rental_policy.py` (guarded operator rentals), `fleet_store.py`
(rental records, rig observations, log chunks), `rig_ssh.py` (validated SSH
transport), `telemetry.py` + `telemetry_policy.py` (enrolled-rig polling and
presentation), `rig_logs.py` (miner-log tail), `tui.py` + `tui_data.py`
(TUI and its read-only views), `clipboard.py`, `formatting.py`; rig side in
`rig/` (`bootstrap.sh`, `agent.py`, `qubic_parse.py`).

### Rig runtime: Clore's HiveOS image + bootstrap

No image of our own. Rentals use Clore's own **`cloreai/hiveos:0.4`** image
(the HiveOS image pulls fast on Clore, most likely because hosts have it
cached), and the order's `command` replaces its HiveOS startup with a small
bootstrap that fetches our agent. Everything else — agent and miners — is
downloaded at start, the same way HiveOS fetches custom miners.

- **What `cloreai/hiveos:0.4` contains** (inspected locally): 293 MB
  compressed, 277k pulls. Ubuntu 24.04; CUDA 11.7 runtime + `cuda-compat-11-7`
  + `libnvidia-compute-515`; HiveOS userland in `/hive`; python 3.12, jq,
  curl, sshd, screen. Env already sets `NVIDIA_VISIBLE_DEVICES=all` and
  `NVIDIA_DRIVER_CAPABILITIES=compute,utility`. Default `CMD` is
  `/scripts/entrypoint.sh`, which writes `/hive-config/rig.conf` from
  `rig_id` / `rig_pass` env vars and runs `hive`. Our `command` skips that,
  so HiveOS never starts.
- **Bootstrap** (the order's `command`, a short shell one-liner kept in
  config):
  1. download `rig/bootstrap.sh` from the public repo
     **`github.com/dcogswell/clorqub`** at a **pinned commit**
     (`https://raw.githubusercontent.com/dcogswell/clorqub/<commit>/rig/...`)
     and check it against the SHA-256 in the command;
  2. the bootstrap fetches `agent.py` + `qubic_parse.py` from the same commit
     and checks them against `rig/SHA256SUMS` at that commit;
  3. lock down the image's defaults (see Security below), start `sshd`
     key-only;
  4. exec `agent.py`.

  Updating the agent = new commit + new hashes in config; running rentals
  keep their version until replaced.
- **Security of the base image:** it has a `user` account with password `1`
  and passwordless sudo. Before `sshd` starts, the bootstrap locks that
  password (`passwd -l user`), sets `PasswordAuthentication no` and
  `PermitRootLogin prohibit-password`, and installs only the clorqub public
  key.
- **GPU / CUDA:** qli requires **CUDA 12+ drivers (535+)** and its GPU
  trainer is `CUDA12`; jetski downloads `JETSKI-TRAINER-GPU` /
  `JETSKI-TRAINER-GPU-AMD` from `mirrordl.jetskipool.ai` at runtime.
  - **Host drivers are fine:** every available Clore server reports
    `cuda_version` 12.2–13.5 (1455 on 13.x, 190 on 12.x). A
    `min_cuda_version` filter (default 12.0) still guards the score.
  - **Verified on the test rental:** GPU mining works on the image as is
    (RTX 3060, host driver 620.70, jetski GPU 84–85 it/s, qli GPU 41+ it/s
    while ramping). The GPU trainers have no CUDA library dependencies, so
    the image's baked CUDA 11.7 / `libnvidia-compute-515` don't matter.
  - Do **not** pass `NVIDIA_VISIBLE_DEVICES` / `NVIDIA_DRIVER_CAPABILITIES`
    in order `env`: Clore refuses the order (`code 1`); the image already
    sets them.
- **CPU-only rentals** don't touch CUDA, so they work on the image as is.
- **Fallback image (not needed so far):** `command` does override the
  image's startup and GPU works, so this is only for future problems:
  build `ghcr.io/dcogswell/clorqub-miner` `FROM cloreai/hiveos:0.4`
  with a tiny layer (agent + `CMD`). Hosts that cache the base then pull
  only that layer. Pushing needs a GitHub token with `write:packages`
  (`GHCR_TOKEN` in the env file).
- **Miner packages, fetched on the fly** (URLs in config `packages`, one per
  pool; defaults are the three `latest` URLs in "Phase 0 findings"):
  1. At start, the agent downloads the active pool's package first, verifies
     its SHA-256 against the `.hash` URL, extracts it, writes
     `appsettings.json`, and starts the miner. It then downloads the other
     pool's package in the background, so a later switch doesn't wait for a
     download.
  2. Every download records URL, version (`CUSTOM_VERSION` from
     `h-manifest.conf`), SHA-256, size and download time in `status.json`.
     The daemon stores them per pool run, so a new release isn't mistaken
     for a rig getting faster or slower.
  3. **Updates:** the agent checks the `.hash` URL every
     `package_check_interval` (config, default 1h). If a new release appears,
     it downloads and verifies it, then restarts the miner on it at the next
     safe point (not inside a measurement the daemon has flagged as in
     progress). `auto_update` can be turned off in config to hold a version
     for a while.
  4. A hash mismatch or failed download is retried, then reported; it never
     runs an unverified binary.
- **jetski solo vs PPLNS** is config only (same binary): `jetski_mode =
  pplns | solo` sets `pool.url` and `pps`. Default `pplns` (matches the local
  rig). The package URL used is the matching one, so the version stays in
  step with jetski's releases.
- **Order:** `create_order` with `image=cloreai/hiveos:0.4` (configurable),
  `command=<bootstrap>`, `ports={"22":"tcp"}`,
  `ssh_key=<clorqub public key>`, and `env` with only
  non-secret settings: `POOL=jetski|qli`, `ALIAS=cq-<server_id>-<order_id>`
  (short `cq-` prefix: aliases are visible on pool dashboards, so they don't
  name the project),
  `CPU_THREADS` (from `specs.cpus`, minus 1), `GPU=on|off`, `JETSKI_MODE`,
  and the package URLs.
- **Credentials are pushed over SSH, not put in the order.** Order env is
  stored by Clore and shown in its API/UI, so neither credential goes there.
  Once SSH is up, `rig.py` writes `/clorqub/credentials.json` (mode 600):
  - `jetski_wallet` -> jetski `pool.wallet`;
  - `qli_access_token` -> qli `ClientSettings.accessToken`.

  The agent waits for that file before starting a miner, and renders each
  miner's `appsettings.json` from it. The early-boot timer allows for this
  step.
- **Exposure:** the host operator can read anything in the container, so
  the qli token is exposed to every host we rent. Use a token dedicated to
  clorqub if qubic.li allows several, so it can be revoked without touching
  the local rig. The jetski wallet is a public address; the risk is only
  that it identifies the user's wallet.
- **Agent:** supervises the active miner, restarts it on crash, and writes
  `/clorqub/status.json` every few seconds: pool, package version + SHA-256,
  miner pid/uptime, it/s, shares (log + `.lock`), trainer version, epoch,
  and a **boot timeline** (container start, package download start/end,
  trainer runner download start/end, first it/s, first share). For jetski it
  reads `127.0.0.1:17899/summary`; for qli it parses the log.
  It also reports actual hardware (`nproc`, `/proc/cpuinfo` model,
  `nvidia-smi` GPU list) and mirrors `.lock` files for both miners.
- **Control over SSH** (dedicated key pair at `~/.config/clorqub/id_ed25519`):
  `rig.py` reads `status.json`, copies `.lock` files, and runs
  `agent switch <pool>` to switch pools. No inbound API on the rig.
- **Pull time** counts against the 10-minute free window; the boot timeline
  records it per rental, so hosts without the image cached show up.

### Process model

The daemon spends money unattended, so it is never tied to a terminal: it runs
headless under systemd. Every UI is a separate client process.

- **The daemon is the only writer** of rentals, stats snapshots and decisions.
  SQLite WAL mode lets clients read while it writes.
- **Clients read SQLite.** They can start, stop or crash without affecting the
  daemon, and the daemon needs no API or socket server.
- **Control goes through a `commands` table.** Clients insert rows (pause,
  resume, set-budget, set-epoch-budget, set-max-rentals, blacklist, release,
  switch-pool, ...);
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
  drill-down, hardware reference, decision log, command entry. Runs locally
  or over SSH.
- **Web view** (later): FastAPI reading the same file, bound to localhost or
  behind auth.
- **Discord bot** (future phase, user already has a server): posts `events`
  and accepts commands from allowlisted Discord user ids in designated
  channels. Same `commands` / `events` path as every other client, so no
  daemon changes are needed to add it. The bot token is an env var.

### Future phase: local non-rental PCs

- Monitor the user's own mining PCs alongside Clore rentals. Start
  **read-only**: collect hardware, miner/pool/worker identity, trainer
  versions, uptime, CPU/GPU rates, shares, epoch and health through an
  explicitly configured local agent or SSH connection. Do not replace or
  restart an existing miner just to collect stats.
- Give each machine a stable device id and `local` source type; rentals
  retain their Clore order/server ids. Reuse measurements, pool runs,
  epoch reports and durable counter capture without inventing a Clore
  order or counting a local PC against rental-slot limits.
- TUI/reporting show Rentals and Local PCs separately, with optional
  combined pool/epoch totals. Deduplicate worker/share observations and
  preserve worker attribution when manually entered pool earnings cover
  both local PCs and rentals.
- Local PCs incur no Clore rental charges and do not consume the rental
  daily/epoch budgets. Optional measured power and electricity prices may
  supply a separate local operating-cost view; unknown local cost is
  **unknown**, not free, and must not create a bogus best USD/QUBIC score.
- Missing telemetry raises a local-PC health alert, not a rental release
  or cancellation action. Any future local pool switching/restart controls
  require explicit opt-in per machine and must respect other miner
  supervisors. Keep connection credentials outside SQLite and logs.
- Deliver after the rental collector/reporting path is stable; this
  future phase is not implemented by the current marketplace viewer.

### Clore billing

- **Prices:** the API's `price.usd.on_demand_usd` is **USD per day**. Spot
  bids are per-currency (`spot_marketplace` gives `min_pricing` and competing
  `offers`); `spotprice` in `create_order` is per day in the order's
  currency. A new bid must be ≥1% above the top bid (`below_min_raise_step`
  returns the lowest accepted price); a bid below the floor returns
  `too_low_price` with `min_price`. Lowering a spot bid is limited to once
  per 600 s.
- **Renter fee** on top of the price, from `GET renter_fees`: **5%
  on-demand, 1.25% spot** (base; the account's `extra` is 0). The web UI's
  $/h = API $/day ÷ 24 × (1 + fee) — checked on 5 servers. So
  `cost_usd_per_hour = price_per_day / 24 × (1 + renter_fee)`.
- **Creation fee**, also from `renter_fees` (USD-Blockchain): **$0.005 spot,
  $0.10 on-demand**. Charged only if the order runs past 10 minutes: the
  test order cancelled at 75 s was billed for time only. Read the fees live
  from `renter_fees` rather than hardcoding them.
  - These are small next to hourly prices, so the fee matters mostly for
    churn: a few cents per swap. The bigger cost of replacing a rig is the
    unpaid image pull, boot and trainer-download time.
  - The creation fee is a sunk cost once paid. Scoring amortizes it over the
    expected rental length; rotation counts it as a cost of replacing a rig.
- **Early boot check (inside the free window):** after ordering, the daemon
  checks quickly. If any of these fail by about the 8-minute mark, it cancels
  before the fee applies:
  - the order is `running` and SSH to the container works (image pulled);
  - the agent reports the miner running and it/s > 0;
  - the reported hardware matches the listing (threads, CPU model, GPUs);
  - the network is usable: trainer download done within
    `max_trainer_download_seconds` and the miner connected to the pool.

  The rule is "miner started and hardware matches", not "shares produced".
  (Superseded in part: see "Benchmarking and auto-rental" for the planned
  first-share gate.)
  Every timestamp in the boot timeline is stored, so slow pulls and downloads
  are visible per server and per country.
- **Spend tracking** uses what Clore actually charged (fees + rental), not
  price x hours. Clore prices in CLORE/BTC, so USD cost moves with the
  exchange rate; store both the native amount and the USD rate at the time
  (`currency_rates_in_usd` from the API).
- Every order is placed with a **maximum duration** (where the API allows; the
  listing's `mrl` is the host's cap), so a rental can't bill forever if
  everything else fails.
- **Auto-renew is strictly opt-in** (`rent.py create --auto-renew`, or the
  TUI setup checkbox — default off): it asks Clore to re-bill each `hours`
  term until cancelled, so the quoted cost bound covers ONE term only and
  the review screen says so. The receipt must echo `auto_renew: true` or
  the order is auto-cancelled as a term mismatch. The TUI's **Max** hours
  button fills the listing's `mrl`; `fleet_rentals.auto_renew` (0/1/NULL)
  records what was ordered.
- **Renew toggle on live orders:** the Fleet detail's **Auto-renew**
  button calls `set_order_auto_renew` (one POST, never retried) on an
  active on-demand order — always with `auto_renew_max_price` when
  enabling, so renewal stops if the host raises the daily price.
  Spot, ended and observe-only orders are refused locally;
  `renewal_in_progress` in a term's last 60 s is not retried.

### Network / region filter

Servers in **Russia and Ukraine** often have major bandwidth problems
(slow trainer downloads, dropped pool connections). Together they are 39% of
available servers.

- `country_blocklist` in config (country = `specs.net.cc`), default
  `["RU", "UA", "BY"]`: these servers are dropped before scoring, like
  blacklisted servers. Editable via config or a command. Belarus was added
  2026-11-15 after the first experiment rig there (order 2445525) failed
  about half its SSH reads while mining.
- **Listed speed is stale** (median speed test 52 days old).
  `min_network_mbps` is only a loose pre-filter for very low listings, off by
  default.
- **Measured download time is the real signal.** Outside Russia the trainer
  download is almost instant; Russian rentals often take several minutes. The
  boot timeline gives image pull and trainer download time per rental,
  server and country.
  - Early boot check: a download not finished after
    `max_trainer_download_seconds` (config, default ~120s) fails the check, so
    the rental is cancelled inside the free window.
  - A server's measured times are kept and used for future scoring, replacing
    its listed speed.
- Per-country stats (early-check failures, pull/download time, pool
  disconnects, stalls) are kept in SQLite, so `cli.py report` can show other
  regions that do badly; adding them to the blocklist is a manual decision.
- Network problems are an auto-blacklist reason for the server itself, even
  outside blocked countries.

### Local stats capture

The miners reset their counters on restart (including every pool switch),
qli deletes its own stats files, and a pool only knows about shares sent to
it. clorqub therefore keeps **its own** history in SQLite, keyed by rental,
and never relies on a remote counter staying alive.

- `collector.py` polls every active rig's `status.json` over SSH on a fixed
  cadence and stores a **raw snapshot** (source, timestamp, rental, worker,
  pool, raw JSON) plus parsed fields: it/s, accepted/rejected/found/invalid
  counters, uptime, trainer version, epoch. `.lock` files are copied on a
  slower cadence.
- **Counter resets:** a counter that goes down means a reset (switch,
  restart, rollover). The delta is the new value, not a negative number.
  Share events are derived from these deltas; `.lock` `Solutions[]` (deduped
  by `SolutionHash`) are authoritative when available.
- **Final snapshot before any switch or release:** before the daemon switches
  a rig's pool or releases a rental, it takes one last snapshot (`status.json`
  and the `.lock` files), so the tail of a run isn't lost.
- Pool-side stats (jetski / qubic.li APIs, by worker alias) are stored as
  separate snapshot sources, so rig-reported and pool-reported numbers can be
  compared.
- Raw snapshots are kept (with a configurable retention) so derived tables
  can be recomputed if a parser bug is fixed.
- **Miner-log archiving (current):** each 30 s telemetry tick appends the
  new bytes of `/clorqub/logs/<pool>.log` into `rig_log_chunks` with a
  per-rig/pool byte cursor (`rig_log_cursors`); shrink/rotation re-reads
  the tail. The `l` log view falls back to this archive when the live
  SSH fetch fails, so logs survive rig death and web-UI cancellation.
- **Storage growth finding (2026-11-15, decide at experiment end):**
  `rig_log_chunks` is the largest table (~22 MB of a ~49 MB db) and grows
  ~0.8 MB/hr per rig (~95 MB/day at ~6 rigs) -- the 1 s `SHARES:` heartbeat
  dominates while the analytically valuable lines (tree `ID:` transitions,
  `Share found`, errors, restart banners) are ~1% of volume. Candidate
  strategies, to be chosen when the experiment phase ends:
  (a) materialize tree events into a `rig_tree_events` table, then drop
      chunks for rentals ended >24 h;
  (b) cap retained bytes per active order (~2 MB tail for ad-hoc views
      and parsing);
  (c) zlib-compress chunks (~8x) if raw retention is still wanted;
  (d) `PRAGMA wal_checkpoint`/`VACUUM` after deletes so space returns to
      disk. Until decided, expect ~3 GB/month of log growth at current
      rental scale.
- **Stock `miner`/`miner log` on the rig (current):** the agent runs the
  miner under a pty, writes the raw ANSI stream to
  `/clorqub/logs/<pool>.raw.log`, and points HiveOS's stock commands at
  it -- a detached `miner` screen session tails it for `miner`, and
  `/run/hive/miner.1` symlinks to it for `miner log`.
- **Vanished orders:** a managed rig absent from a fresh (<15 min) good
  `my_orders` snapshot whose SSH is failing is marked `missing` and no
  longer polled. This is detection only; it does not cancel anything.
- **TUI fleet/orders views:** the Fleet tab lists only active rigs by
  default (cancelled/missing/failed states and orders vanished from a
  later good inventory are hidden) and shows burn $/hr plus reported
  spend; `f` toggles to all rigs, with ended rigs dimmed and their
  uptime frozen at the end time. The Orders tab keeps every order ever
  seen in snapshot history, marked active or ended, with Sh/h (recent
  submitted shares/hour from rig telemetry) and Sh/¢ (shares per
  fee-inclusive cent of spend rate).
- **Finding (2026-11):** a rented rig was unreachable for ~78 min while
  still billing before the user cancelled it by hand -- and its miner
  log was lost because nothing persisted it. Log archiving now covers
  the log half; **unreachable alerting / auto-cancel policy** (e.g.
  alert or auto-release after N minutes of failed SSH on a still-billing
  order) is a future safety item, not implemented.

### Hardware reference

Every server's CPU and GPU details are kept in SQLite for reference, whether
or not we rented it.

- `servers`: clore `server_id`, `owner` (hoster id), country, first/last
  seen, latest listing.
- `server_specs`: one row per **observed spec interval** for a server
  (valid_from / valid_to), so a host swapping hardware or changing the threads
  offered is visible: CPU model, `cpus` cores/threads offered, RAM, disk +
  speed, motherboard, PCIe rev/width, GPU models (`gpu_array`), GPU count,
  VRAM, CUDA, power limits, listed net down/up + latest speed-test time.
  A new interval opens only on a real spec change: the free-space figure
  appended to `specs.disk` and live GPU power draw (`specs.pl`) are ignored
  when comparing (`market.spec_change_key`).
- `server_prices`: price history (on-demand and spot min, per currency, plus
  USD/day). A new version opens only when an owner-set USD field changes
  (`usd.on_demand_usd`, `usd.spot`, `original_usd`, `USD-Blockchain`); the
  CLORE/BTC amounts Clore re-converts at the live rate every poll are
  ignored (`market.price_change_key`). Each version keeps the full price
  JSON from its first sighting.
- `marketplace_snapshots`: one row per scan (time, source, server count);
  only the newest row keeps the full raw response. `servers.latest_json`
  always holds each server's current raw listing (live FX amounts, disk).
- **Finding (2026-11-15):** storing every 5-minute scan in full grew the db
  ~1.7 GB/day (4.3 MB raw payload per scan; ~99% of price versions were
  FX-only and ~95% of spec versions were free-disk-only). After the
  change-only rules and a one-off compaction of existing history the db
  went from 1.78 GB to ~35 MB; a scan now adds tens of rows, not ~2,500.
- `rig_reports`: what the rig actually reported at boot (CPU model, `nproc`,
  CPU flags such as AVX2/AVX-512, `nvidia-smi` GPUs, driver/CUDA). Compared
  with the listing for the spec-mismatch check.
- `performance_benchmarks`: sourced whole-rig or device-level yield anchors
  keyed by GPU model, CPU profile/ISA, offered thread count when known, pool,
  and trainer/package version. Store point estimates versus lower-bound
  observations, source, sample count, and update time separately. The user's
  initial jetski whole-rig anchors are seeded by the read-only scanner; rentals
  progressively replace broad priors with verified measurements.
- `cli.py hardware` lists models with their measured it/s and how many
  servers/rentals back each number; `cli.py server <id>` shows spec history.

### Per-server tracking and blacklist

Every rental is keyed by its **Clore server id**, so we can see how each
physical server does over time, across rentals.

**Implemented (2026-11-15):** `blacklist.py` stores server blocks with
reason, source, evidence, added/expiry/removal timestamps. Automatic blocks
last **3 days**; manual blocks do not expire. The experiment runner writes
automatic entries for:

- Confirmed fault or unreachable cancellations.
- Failed provisioning after an order was created (not quote, API, or
  bounded-term receipt failures).
- More than 40% failed telemetry reads in the last 30 minutes, with at least
  20 reads and a rental age of at least 30 minutes.
- CPU model-number mismatch, or fewer visible CPU threads than the listing's
  offered allocation (parsed the same way as the order's default thread count).
- CPU or GPU median it/s below 50% of a compatible model prior from at least
  **3 other rentals**, excluding the current order. Requires 30 minutes since
  first telemetry, at least 20 usable readings spanning 20 minutes within the
  last 30 minutes, and excludes the first 2 minutes after start/restart.
  Compare only matching epoch, package, pool, visible CPU count, GPU count
  and trainer variant/version; skip unknown epoch/package/allocation.

Stale collection supplies no new blacklist evidence. Low shares, dry trees
and slow first shares never trigger a block. Flaky SSH means unreliable
controller access, not proof of lost pool shares. Flaky SSH, low it/s, and
late spec mismatches block future automatic rentals without cancelling the
current order. Spec mismatches also join the existing free-window fault check.
New blocks are recorded once per rental; removing one does not immediately
recreate it from that rental's old evidence.

Blocked servers are excluded from automatic fills, probes and re-rents and
from TUI Candidates. Marketplace/server detail shows the reason. Manual
`rent.py` and TUI rental review warns but allows confirmation; country and
other rental safeguards still apply. Use `python3 blacklist.py list [--all]`,
`add --server ID --reason TEXT`, or `rm --server ID`; all accept `--db PATH`.
Dry-run logs proposed blocks without inserting blacklist entries. Restart an
already-running experiment process to load these rules. Existing per-phase
fault/rent-failure exclusions still apply independently of blacklist expiry.

The broader tables and hoster-level policy below remain the target design.

SQLite tables (besides the hardware reference above):

- `rentals`: clore order id, `server_id`, rig image + agent commit,
  start/end, price,
  creation fee, actual charges, spot/on-demand, `burst` flag, boot timeline
  (order created, running, SSH up, trainer downloaded, first it/s, first
  share), end reason (rotated, stalled, interrupted, budget, burst-unwind,
  early-check failed, manual).
- `pool_runs`: one row per (rental, pool, package version) stretch:
  start/end, jetski mode, package URL + version + SHA-256, why it started
  (initial, switch, package update) and ended. Every switch or miner update
  closes one run and opens the next, so yield is attributable to a pool and
  a release.
- `packages`: every package release seen (pool, version, SHA-256, size,
  first seen), so `cli.py report` can compare it/s across releases.
- `snapshots`: raw collector snapshots (see "Local stats capture").
- `measurements`: `rental_id`, `server_id`, `pool_run_id`, `pool`, timestamp,
  epoch, trainer version, it/s, found/submitted/invalid shares, derived from
  snapshots.
- `shares`: individual share events (`worker`, `server_id`, `rental_id`,
  `pool_run_id`, `pool`, `Created` UTC, epoch, invalid, `ReportedScore`),
  deduped by `SolutionHash`. When only counters are available, counter deltas
  between polls are stored as timestamped events.
- `payouts`: per pool, QUBIC amount, time, epoch, source. Feeds
  `qubic_per_share`; reconcile transfers with the epoch earnings ledger
  rather than counting a transfer as new earnings again.
- `epoch_earnings`: manual/API earnings by epoch, pool, window and worker/
  account scope, with estimated/confirmed status, source and correction
  history. Calibrates pool yield only when work/earnings attribution matches.
- `prices`: QUBIC/USD and CLORE/USD samples, source, time.
- `epoch_budgets`: epoch id/boundaries, configured USD cap and audited
  changes, with epoch-attributed actual charges and outstanding reservations
  retained for reconciliation and reports.
- `order_inventory`: timestamped success/failure observations of `my_orders`,
  with sanitized financial/hardware metadata. Unknown orders are observe-only
  until adoption; failures preserve the last good view rather than inventing
  an empty fleet.
- `blacklist`: `server_id` or `owner` (hoster), reason, source (auto/manual),
  added at, optional expiry.
- `commands`, `events`, `decisions`: see "Process model".

Use:

- **Scoring**: blacklisted servers/hosters and blocked countries are dropped
  before ranking. Compare solutions/hour only within compatible pool,
  CPU-ISA/GPU trainer, and version profiles. Blend a server's sustained,
  verified whole-rig yield with a matching prior, weighted by measurement
  confidence. Do not sum raw CPU/GPU it/s or apply an AVX512 prior to AVX2.
- **Exploration**: with only 2 slots, exploration doesn't get a dedicated slot.
  Instead, an untested server whose prior-based score is close to the best
  (within a configurable margin) may take a slot, at most one at a time, so a
  wrong prior for a hardware model gets corrected.
- **Future extensions** (configurable thresholds, with evidence for each trigger):
  - measured it/s < X% of the hardware prior over >= N hours of rentals;
  - failed the early boot check;
  - repeated verified miner stalls (not dry trees) or spot interruptions;
  - invalid/rejected share ratio above a threshold;
  - network problems: slow image pull or trainer download, repeated pool
    disconnects;
  - listed specs don't match what the rig reports (CPU/GPU model, thread count).
  Auto entries expire after a configurable period so a fixed server can come
  back. Manual entries never expire. Repeated auto entries across one
  hoster's servers can escalate to a hoster entry (manual confirm).
- **Reports**: `cli.py server <id>` gives a server's rental history, spec
  history, it/s and shares/hr over time per pool, QUBIC mined, effective cost
  per QUBIC and incidents. `cli.py servers` ranks every server we've rented
  by lifetime QUBIC per dollar.

### Share summary windows

Share counts for **15m / 1h / 2h / 4h / 24h / epoch** (windows in config
`summary_windows`), shown at these levels:

- **fleet**: all active rentals, plus total $/h, QUBIC/h, QUBIC per dollar and
  effective cost per QUBIC vs market price per window, split by pool;
- **per worker**: one row per worker alias (`cq-<server>-<order>`), with
  rental id, `server_id`, current pool, burst flag, hardware, it/s, uptime
  and $/h alongside the windows. Also includes workers clorqub didn't rent
  (e.g. your local rig) if a pool API reports them, marked as unmanaged;
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

1. **Agent `status.json` over SSH**: it/s, shares, trainer state, boot
   timeline. Fast, written by our own code.
2. **`.lock` files over SSH** (agent mirror for both miners): authoritative
   share events, used for audit, final pre-switch snapshots, and calibrating
   the perf model.
3. **Pool APIs**: jetski per-worker stats (by alias) and the qubic.li API,
   where available. Independent of the rig, so they catch rigs that are lying
   or unreachable.

### Pool switching

jetski stays the default pool a new rental boots onto; qli is the alternate.
Both packages are on the rig (the second one is fetched in the background at
start), so a switch is `ssh <rig> agent switch <pool>`
— the agent stops one miner and starts the other; no rental churn.

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
  warm-up period applies again (including the other miner's trainer
  download) before it can be judged.
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
  3. snapshot marketplace (server_id, owner, country, specs, price/day,
     availability) -> servers / server_specs / server_prices; drop
     blacklisted servers/hosters, blocked countries and listings under
     min_network_mbps
  4. collector: snapshot every active rig into SQLite; update perf model
     (it/s per server and hardware model, shares/iteration and QUBIC/share
     per pool)
  5. early boot check on rentals still inside the 10-minute free window;
     cancel failures before the creation fee applies
  6. score all available servers (QUBIC per dollar), apply premium ceiling
  7. fill slots under the spend policy:
     - up to max_rentals with the best candidates;
     - beyond that, up to max_rentals_burst only for high-value candidates;
     - never over either the daily or epoch budget
     -> create_order with cloreai/hiveos:0.4 + bootstrap command, POOL=jetski
  8. for rentals whose yield is well below expected on their current pool but
     whose price is still competitive: final snapshot, `agent switch` to the
     other pool (switch cap / min window), open a new pool_run
  9. release:
     - burst rentals that no longer qualify as high value (first);
     - rentals over the premium ceiling, still underperforming after their
       pool try, or that tripped the adaptive stall detector after warm-up;
     - a rental beaten by an available candidate by the hysteresis margin
       (counting the new creation fee and boot time) when no slot is free
     final snapshot, cancel_order, evaluate auto-blacklist rules for the
     released server
 10. log every decision with its inputs (scores, prices, pool yields, slot
     counts) to `decisions`; emit `events`; heartbeat for the watchdog
```

Clore rate limits (1 req/s, `create_order` 1 per 5 s) are enforced in
`clore.py`; the loop interval is 60 s or more.

## Benchmarking and auto-rental (design, 2026-11-15)

**Implemented (2026-11-15) as `experiment.py`** (with `trees.py` and
`rig_ssh.restart_miner`): phases with a USD budget/reserve, dry-run by
default, `--live` needs `--confirm-budget`. Re-rolls kill the miner over
SSH (`pkill` of `/clorqub/pkgs/<pool>/`); the agent restarts it in ~5 s,
so no agent change. Tier preference: at least half the slots go to Zen 4/5
AVX-512 Ryzens when listed; otherwise least-covered (CPU, GPU) first, then
it/s-per-USD prior. Value-based switching (rule 2) is not active in the
experiment phase. Telemetry comes from the collector; if it goes stale the
runner blocks all actions rather than cancelling rigs. `experiment.py
report` prints per-rental shares/cost, restarts with tree changes and
shares within minutes, tree segments, and per-x/y summaries.

**Burst probing (implemented, user idea):** when a slot rig hits a dry
tree (or on `experiment.py burst`), rent up to 4 extra cheapest-first
probes on 1 h terms, never auto-renew. No restarts on probes; at ~5 min
of mining keep every probe that found a share plus the cheapest
non-finder as a control, and cancel the rest by 8 min, inside Clore's
free 10-minute window. New rentals pause while any probe is within 4 min
of that deadline, because order provisioning can block a cycle for up to
3 min. Auto bursts are at least 60 min apart. The report lists each
burst's probes, their first tree IDs (how many distinct trees concurrent
rigs got) and kept-vs-control shares per USD. Auto-renew stays off as a
strategy: an owner can raise the renewal price, and
`auto_renew_max_price` stopping renewal on a price rise is the API's
documented behaviour, not something we've tested. Telemetry now runs every
15 s (`--tick-interval`) and the runner every 15 s.

The design below replaces the "learned perf
model" assumption that a rig's share rate is a stable property that can be
measured in a short window.

### Algorithm (agreed with user, 2026-11-15)

1. **Rank by it/s per USD:** total it/s (CPU + GPU, as the agent reports
   it) divided by fee-inclusive $/hr. Before renting, it/s comes from our
   measured history for that GPU/CPU model (hardware reference); after
   renting, from the rig itself.
2. **Only switch rentals for better value:** release a rental only when an
   available candidate has clearly better it/s per USD (hysteresis margin,
   counting the creation fee), or the rig is faulty. Share rate alone never
   triggers a switch.
3. **Dry tree -> re-roll:** when a rental has no shares for a set stretch
   (at start-up or mid-rental), restart the miner to try for a new tree,
   repeatedly, for up to **x** time.
4. **Still dry after x -> stop and re-rent:** cancel the rental, wait **y**,
   then rent the same server again if it is still available and still the
   best value (otherwise the next best).

x, y and the "no shares for" stretch are config values; defaults are set
from the experiments below, not guessed. The rest of this section is the
evidence and detail behind these rules.

### Experiment phase first (user decision): data over value

For the first day or two the algorithm runs to **collect data, not to
maximise value**. Where the two conflict, pick the option that teaches more:

- **Hardware coverage:** rent across CPU classes on purpose, not just the
  cheapest. The first 12 rentals were mostly cheap rigs with weak CPUs, so
  CPU it/s is barely sampled. Include AVX-512 Zen 4/5 rigs (7945HX, 7950X,
  9950X) even when pricier, plus a few cheap AVX2 rigs as a control, and
  re-rent the same server where possible.
- **Longer rentals:** several hours each, so whole tree cycles (burst,
  dry, next tree) are observed; no early release on value grounds.
- **Exercise every rule:** start-up restarts, mid-rental restarts on dry
  trees, and stop/re-rent, with x and y deliberately varied, so their
  defaults come from measurements.
- **Log everything:** tree ID/seed transitions, each restart with its tree
  ID before/after, it/s per device, shares, cost, order-to-SSH and
  first-share times.
- **Hard limits still apply:** a phase budget set by the user, balance
  check before each rental, max concurrent rentals, no auto-renew.

CPU it/s measured so far (median after warm-up, agent-reported):

| CPU | Threads | ISA | CPU it/s | Notes |
|---|---|---|---|---|
| Ryzen 9 7950X | 32 | AVX-512 | 46 (max 48) | 13 min run |
| Ryzen 9 7945HX | 32 | AVX-512 | 13 and 24 | two short runs (35, 22 min) |
| i9-10980XE | 36 | AVX-512 (Intel) | 9-11 | incl. the 10 h run |
| Ryzen 9 9950X | 28 | AVX-512 | 9 | 26 min, no usable GPU |
| Ryzen 9 5900X / 3900X | 24 | AVX2 | 7-9 | |
| Xeon E5-2695 v4 | 36 | AVX2 | 3 | |
| i3-10105 | 8 | AVX2 | 1 | |

The user's manual runs put 7945HX/7950X at 30-40 it/s (`USER_RATE_CONTEXT`
records ~35), and the 7950X rental matches. The AVX-512 flag alone is not
the signal: the Intel AVX-512 chip and the 9950X (28 threads offered) were
no better than AVX2 Ryzens, and both 7945HX rentals fell short of 30-40
(short runs; warm-up or thread allocation not yet ruled out). The pre-rental
it/s prior is therefore keyed by CPU model and threads offered, not ISA.

### What the first 12 rentals show (all jetski, epoch 239, package 4.5)

- **Hashrate is a stable rig property; share rate is not.** The 10 h
  rental 2443365 (i9-10980XE + RTX 3080, $0.103/hr) held 184-220 GPU it/s
  with zero miner restarts, while shares per hour went 421, 328, 137, 20,
  28, 28, 28, 75, 281, 282 (1,628 total). In 10-minute windows the same rig
  ranged 0-195 shares. During the 5 h slump the GPU it/s was at its
  *highest*; it dips during share bursts (cause not established).
- **Shares come from the pool's ant-colony "tree", not from raw it/s.**
  jetski logs `E:<epoch> | ID: <id> | SEED: <seed>` whenever its work
  changes. On 2443365 the seed stayed the same all epoch but the ID changed
  7 times, and share rate followed the ID (counts are `Share found` log
  lines, a subset of the submitted counter): YECRTS (02:06-03:51, 187), ATYLHH (0.5 h, 44), ZUNPZY (04:21-07:21, 11), AXASCB (1 h, 4),
  UBHBBP (1 h, 2), HRIKFL (09:31-11:46, 116). User model: once a vein is
  mined out, nothing more comes until the pool sends a new tree. *Hypothesis:
  the logged ID identifies the tree.*
- **Rigs running at the same time had different IDs** (2442490 UTRUSQ vs
  2442569 GOSJBG, 21:00-21:40 on Nov 14), and their bursts did not line up.
  So even concurrent rigs are not on the same tree. Per user: comparing rigs
  is only meaningful on an identical tree.
- **Hourly shares do not follow it/s.** i3-10105 + 3080 (83 GPU it/s) did
  486 sh/h; i9-10980XE + 3080 (197 it/s) did 328 sh/h, each over its only
  hour. Tree luck dominates short windows.
- **Start-up is bimodal** (user experience, matches the data): 9 of 12
  rentals had a first share within 2 min of the miner starting. 2442776 took
  16.5 min; 3 had none after 8-22 min and were cancelled. (The rig agent
  starts the miner within ~1.5 min of our record; true order-to-SSH time is
  not measured yet.)
- **No pool API exposes pool-wide or per-tree share data** (user), so tree
  state has to be inferred from our own rigs' logs.

### Model

- **Score = shares per dollar over whole trees, not per hour of a rig.** A
  rig's value is (its share of a tree) x (how often it gets rich trees) /
  price. Short-window sh/h is not used for ranking or for dropping a rig.
- **Hardware check, not yield check, for trials.** it/s settles within
  minutes and is rig-specific, so it is what a trial measures. It is a
  necessary condition (miner runs, GPU used, hardware matches the listing),
  not a yield prediction.
- **The tree matters more than the rig (user view), so value is it/s per
  USD, not shares per hour.** A rig must pass the hardware bar (miner runs,
  it/s in the expected range for its GPU, listing matches). Hourly shares
  did not follow it/s in our data, which supports not ranking on short
  share windows.
- **Rig comparisons only within one tree ID.** When two of our rigs ever
  report the same ID at overlapping times, that window is a valid
  head-to-head comparison; otherwise rigs are compared only by price and
  it/s.
- **Every measurement is keyed by epoch + package version/SHA + tree ID.**
  The pool algorithm can change any epoch but often lasts a couple. Results
  carry forward while the epoch's algorithm (package version and miner
  behaviour) is unchanged; a change marks all yield-derived numbers stale
  until re-measured. it/s per hardware model carries forward only if the
  package version is unchanged.

### Using Clore's free 10-minute window ("creation")

On-demand creation fee ($0.10) and spot ($0.005) apply only if an order
runs past 10 minutes; cancelling earlier costs seconds of rent only (see
"Clore billing"). With bimodal start-up this window is the cheapest lever:

- **Keep a well-priced rig and re-roll the tree (user decision):** a miner
  restart is preferred over moving to the next candidate. With no share
  yet, restart the miner every ~2 min (at ~2, 4, 6 min). The early boot
  check only *cancels* for rig faults: SSH/agent not up, it/s 0 or far
  below the GPU's range, or hardware not matching the listing. This changes
  the rule above ("miner started and hardware matches", not "shares
  produced") by adding re-rolls, not a share requirement.
- **The ~8 min decision** for a healthy rig with still no share: if its
  price is good (top of the ranking, or within a margin of the best
  available), keep it past 10 min, accept the creation fee and keep
  re-rolling; otherwise cancel inside the free window. A false negative
  under the cancel branch (e.g. 2442776, first share at 16.5 min) costs a
  candidate, not the server: it stays eligible.
- **Restart cadence:** ~2 min per roll; inside the window each roll is
  free. 9 of the 12 rentals had a first share within 2 min of the miner
  starting, so 2 min is a fair wait per roll. Whether a restart
  actually gets a new tree is experiment 2; the gate doubles as that test
  (log the tree ID before and after each restart).
  - On the stock HiveOS image, Ctrl+C in the miner terminal makes HiveOS
    restart the miner. On our rigs the `miner` screen session is only a log
    viewer, so Ctrl+C there kills the viewer, not the miner. The agent
    already restarts jetski 5 s after it exits (backoff doubling to 60 s,
    reset once it is running again), so a deliberate restart needs only a
    small agent change: a `restart` request in `/clorqub/control.json`
    (like the existing `switch_to`) that stops and restarts the miner,
    counted separately from crash restarts, with no re-download.
- Count re-rolls per server; a server that fails the gate repeatedly is a
  hardware/network suspect, not a tree-luck case.

### Dry trees during a rental

- Track per rental: current tree ID, when it started, shares on it. A
  **dry tree** is one with ~0 shares for a configurable stretch after an
  initial burst. It is not a rig fault: no rotation or blacklist because of
  it.
- **Miner restart does NOT re-roll the tree (confirmed 2026-11-15, epoch
  239).** The assignment is held pool-side per worker identity
  (wallet+alias): across 5 restarts of dry rig 2445525 the miner
  reconnected as `rig-deer-96b3` and got the same tree `ID` back every
  time, with the same `SEED`. The miner log shows the pool pushing
  "Ant stream closed; active work cancelled ... durable result retained"
  when IT rotates the assignment. This is deliberate anti-churn design by
  the operator.
- **New draw requires a new worker identity** (different alias, or a new
  wallet) or a new rental — not a new IP or a process restart. Burst
  probes work as tree draws because they are new aliases.
- **Rotation is pool-scheduled, not a fixed ~70 min cadence.** Observed
  assignment lifetimes across 21 rigs' logs: ~30 min to ~3 h, clustered
  ~55-100 min (2443365: 100m/30m/185m/60m/65m/140m; 2445525:
  ~40m/74m). Likely ends when the work unit is exhausted, not on a wall
  clock. Multiple workers can share one tree (4090 rig 2445662 and
  2442295 both held WQIOFC...DAK concurrently).
- Policy implication: `restart on dry` is dead code for tree luck — every
  restart costs ~30 s downtime for zero benefit. Keep miner restarts only
  for genuinely wedged miners (zero it/s). The only real lever for a dead
  tree is paying for a new draw (new rental/worker) vs waiting out the
  pool's own rotation.
- The share **curve** for a whole rental (early burst, slump, second burst)
  is recorded so the pattern can be checked across rentals rather than
  assumed.

### Experiments before any auto-rental logic

1. **Tree ID = tree?** Parse ID/seed transitions from archived logs for
   every rental; check that share-rate regime changes line up with ID
   changes. Free (existing logs). → Confirmed: share regime tracks `ID`
   changes; the `SEED` is constant per worker.
2. **Does a miner restart get a new tree?** On a rig in a dry tree,
   restart the miner once and see whether the ID changes and shares
   resume. → Answered 2026-11-15: NO. Assignment is server-side durable
   per wallet+alias; 5/5 restarts returned the same tree.
3. **Do fresh rentals get a rich tree first?** Across all rentals, compare
   shares in the first tree ID with later IDs. → Partial: burst probes
   (fresh aliases) found shares while the slot rig stayed dry — proving
   dryness is assignment-specific, not pool-wide. No evidence yet that
   fresh draws are favoured above a random draw.
4. **Same tree, different hardware:** only possible if two of our rigs are
   ever given the same ID; log and use any such overlap automatically.
   → Happened: 2445662 (4090) and 2442295 shared WQIOFC...DAK.

### Auto-rental (after the experiments)

Builds on rent.py's guarded flow and the existing Phase 4 rails, plus:

- Check the wallet balance before every rental and stop cleanly with
  enough left to cover outstanding orders (the 10 h rental ran until the
  balance was exhausted).
- Hard daily budget, max concurrent rentals, no auto-renew until the
  scoring has been validated on real data.
- Candidates ranked by price after the hardware bar; yield priors from
  tree-level history once it exists; restart re-rolls on every new order
  and on dry trees, with cancel/release driven by rig faults and price.

### Open questions

- Defaults for x (re-roll time), y (wait before re-renting) and the
  "no shares for" stretch. Does re-renting the same server after y get a
  fresh tree where restarts did not (experiment 3)? Risk: someone else
  rents the server during y.
- Are CPU and GPU it/s the same unit of work for jetski (can they be summed
  for it/s per USD)?
- Is the logged `ID` the colony tree, and does the pool assign it per
  connection, per rig, or per worker alias?
- Does restarting the miner (or reconnecting) get a new tree?
- How long do dry stretches last, and are they periodic?
- *Resolved:* the ~700 sh/hr the user saw on 2443365 was a short-window
  rate at 30-40 min uptime. The rig counter matches: 732/h over the
  10 minutes ending at 40 min, versus 421 shares in the whole first hour.
  Other rentals show the same swing (0 to ~790/h between adjacent
  10-minute windows), another reason not to judge on short windows.

## Safety rails

- **Dry-run default**: prints what it would rent/release; `--live` required to
  spend money.
- **Rental limit**: `max_rentals` (default 2), hard ceiling
  `max_rentals_burst` for high-value rentals only.
- **Budget caps**: daily and per-QUBIC-epoch USD limits, checked against
  actual charges plus unbilled/pending reserves; both apply, burst included.
  Forecast cancellation latency so releasing only after a cap is crossed is
  not the normal control path.
- **Premium ceiling**: no rental above `market_price x (1 + max_premium)` per
  QUBIC; fails closed if the market price is unavailable.
- **Dead-man switch**: `watchdog.py` (separate systemd unit) watches the
  daemon's heartbeat. If the daemon stops checking in for a configurable time,
  it cancels all clorqub-managed orders (and alerts via `events`). It also
  cancels orders it doesn't recognize, if configured to. Together with the
  maximum duration on every order, this stops a dead daemon from billing.
- **Grace periods**: early boot check inside the free window; after that,
  don't judge a rig before trainer download time and enough shares for a
  measurement.
- **Hysteresis**: only replace a rig when the replacement beats it by a
  margin that covers the creation fee (spot or on-demand rate) and unpaid
  pull/boot time.
- **Epoch awareness**: Qubic epochs roll over weekly. Don't judge a rig on data
  that spans a rollover or a trainer update (reuse the epoch-merge logic).
  Also check for Qubic idle periods so they aren't read as fleet-wide stalls.
- **Pool-switch discipline**: min measurement window, per-rental switch cap,
  final snapshot before every switch.
- **Command validation**: the daemon validates every queued command (budget,
  rental limit, dry-run, actor allowlist); spend-increasing commands need
  confirmation.
- **Decision log**: every action + rationale persisted, so behavior is auditable.
- **Package integrity**: every miner package is verified against its
  published `.hash` before it runs; the version and SHA-256 are recorded per
  pool run.
- **Rig hygiene**: agent fetched at a pinned commit and hash-checked; the
  base image's default password locked and SSH key-only before `sshd`
  starts; no secrets in order env or `command`; the rig SSH key is dedicated
  to clorqub.
- Secrets (`CLORE_API_KEY`, `QLI_ACCESS_TOKEN`, later the Discord bot
  token) only in `~/.config/clorqub/env` (mode 600) / env vars, never in
  config, SQLite, logs, the image or Clore order env. The qli token reaches
  rigs only over SSH (see "Rig runtime").

## Phases

| Phase | Deliverable | Depends on |
|-------|-------------|------------|
| 0 | Done so far: Clore API auth/rate limits/endpoints, marketplace field meanings (cross-checked with web UI), price units, spot data, region/network data, HiveOS dropped, miner packages (URLs, `.hash` verification, config formats, jetski local API). **Remaining:** (1) Docker order specifics — `ssh_key` / `command` / max-duration fields, time from order to SSH, whether hosts allow mining in Docker orders; (2) creation-fee rule and actual-charge reporting, via one cheap test rental (with user approval); (3) a real jetski `/summary` response and GPU runtime needs (both binaries already confirmed to run in plain `ubuntu:24.04`); (4) jetski and qubic.li per-worker stats and payout APIs; (5) QUBIC/USD price source; (6) Qubic idle-phase schedule | — |
| 1 | **In progress:** read-only on-demand marketplace scanner (`cli.py`/`market.py`) persists the latest raw marketplace response, change-only server/spec/price history (FX-only price moves and free-disk/power-draw spec moves are not new versions), and user-supplied whole-rig benchmark anchors in SQLite; provisionally ranks only matching RTX 3070 / 3080 Ti / 3090 Ti + Ryzen 9 7945HX or 7950X profiles by sols/hour per dollar. Opt-in `--watch`/`--interval` rescan loop (`scanner_watch.py`, 300 s default, never auto-started) plus `collector.py` controls for a transient systemd-run `--user` service (no install/autostart) feed the read-only `tui.py` viewer. Live scans also poll `my_orders` (observe-only inventory incl. manual web-UI rentals); optional `--spot-limit` adds bounded spot-floor observations (indicative only, no bids). Guarded operator rentals exist via `rent.py` (dry-run default, confirmed --live, on-demand bounded term, one POST, reconcile/adopt, observe-only enroll), and the TUI runs the same guarded flow in-process on `b` (setup -> live quote review -> explicit Confirm & Rent, no terminal handoff; the progress log can be hidden and reopened with `b` while the rental keeps running, and quit is blocked until it finishes) plus one on-demand read-only miner-log tail fetch on `l`; watch/collector cycles also SSH-read enrolled rigs every 30 s into `rig_observations` shown on the TUI Fleet tab (no extra Clore API calls). Remaining: live spot ranking, pool payout conversion, broader CPU/GPU/thread-allocation calibration, and price-premium display | 0 |
| 2 | `qubic_parse.py` (copied parsers for both miners + tests against real `stats.<epoch>.lock`/log samples) + rig bootstrap + agent (package fetch/verify, both miners, `.lock` mirror; tested locally in `cloreai/hiveos:0.4`) + SQLite schema incl. `commands`/`events` + collector + single-rental path: rent one server, early boot check, confirm shares — on each pool, with a switch between them that loses no data | 0 |
| 3 | Share summary windows (15m/1h/2h/4h/24h/epoch; fleet / worker / server, split by pool run) + per-server history + learned perf model + payouts + blacklist (manual + auto rules, `servers`/`server <id>`/`hardware` reports) | 2 |
| 4 | Full loop: spend policy (2-rental limit, burst, premium ceiling, daily + epoch budgets with reserves/rollover accounting) + exploration + pool switching + rotation with hysteresis + watchdog | 1, 3 |
| 5 | Textual TUI client (reads SQLite, sends `commands`). **Partial, delivered early:** `tui.py` + `tui_data.py` over the scanner db: Candidates / Marketplace / Benchmarks / Orders / Fleet tabs, server/order/rig detail, guarded rent and order cancel/auto-renew modals, miner-log view, click-to-sort columns, column nudge buttons and drag scrolling, and a compact layout for small (phone/SSH) viewports. Share windows and `commands` controls still come later | 4 |
| 6 | Later: web view (FastAPI, localhost/auth); Discord bot (posts `events`, accepts allowlisted `commands`) | 4 |
| 7 | Future: read-only local non-rental PC monitoring; shared stats/epoch earnings with explicit machine identity and separate local/rental costs. Optional local controls only after per-machine opt-in | 2, 3, 5 |

Phase 1 is usable standalone — it answers "which Clore rig mines the most
QUBIC per dollar right now, and how does that compare to buying" without
spending anything.

## Open questions

- Is the Clore server id stable across re-listings? (`owner` hoster id is
  confirmed and enables host-level blacklisting.)
- `create_order` for Docker: exact field names for SSH key, startup command
  and max duration; how long from order to container running + SSH reachable
  (image pull), and whether that leaves room for the early boot check inside
  10 minutes.
- Do hosts allow mining in Docker rentals (ToS or per-host flags), or only via
  the HiveOS image?
- Spot after an outbid: does a `paused` order resume on its own when the
  outbidding rental ends, and is the container's filesystem (agent
  `state.json`, packages) kept? When should the daemon rebid (≥1% above
  the top bid) vs cancel and rent elsewhere?
- qli settled it/s on GPU and CPU (the test order was outbid while qli was
  still ramping).
- How often spot rentals get outbid at the minimum bid, per server/GPU
  class, and whether on-demand is worth its 5% fee + $0.10 creation fee
  for stability.
- qli access token: can qubic.li issue more than one (a dedicated,
  revocable clorqub token)? Does it give per-worker stats by alias?
- Does jetski expose per-worker stats and payout APIs? If not, rig stats +
  wallet payouts only.
- Does qubic.li expose a per-worker stats API suitable for yield measurement?
- QUBIC/USD price source (exchange or aggregator API); CLORE/USD comes from
  `spot_marketplace` `currency_rates_in_usd`.
- GPU vs. CPU share yield on each pool: decides when GPU mining is worth it.
- jetski `127.0.0.1:17899/summary`: capture a real response to fix the
  fields the agent reads (does it include share counts, or only versions and
  hashrate?).
- Confirm qli's `.lock` deletion behavior also happens inside the container.
- Qubic idle phases exist (qli has `Idling` settings): on what schedule, and
  do they need a "no shares expected" window in stall detection?
