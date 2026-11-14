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
  enough of it, otherwise the hardware-model prior (see "Hardware reference").
  it/s settles within minutes, so rigs are judged on it, not on share counts,
  which are noisy.
- `shares_per_iteration(pool, epoch)`: pooled across every rig on that pool,
  which gives far more data than any single rig.
- `qubic_per_share(pool)`: from actual payouts. qli pays per share (PPS).
  jetski has solo and PPLNS endpoints (currently the same server per their
  Discord); start with a rough estimate and refine it from payouts.
- `cost_usd_per_hour`: Clore price (API gives USD **per day**; divide by 24)
  + amortized creation fee (see "Clore billing").

Ranking is in QUBIC per dollar, so it doesn't depend on the QUBIC/USD price.
The market price is only used for the premium ceiling and the high-value test
below.

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
- Market price comes from an exchange/aggregator API, cached; if it is
  unavailable, the ceiling and burst tests fail closed (no new rentals beyond
  `max_rentals`, no burst).

Poll the marketplace, rank by QUBIC per dollar, rent the best rigs within the
spend policy, boot them with the clorqub miner image, measure, switch pools on
rigs that aren't yielding, and rotate out underperformers.

## Decisions (from user)

- **Objective: accumulate QUBIC.** Rank by QUBIC per dollar; slight loss vs
  market price is OK; spend limited by rental count + daily budget (see
  "Spend policy").
- **Rental limit: 2 concurrent**, extendable only for high-value rentals.
- **Pools: jetski (primary) + qli/qubic.li (secondary), switchable per rig on
  yield.** jetski: `qubjetski-Client`, `wss://pplns.jtskxpool.ai/ws/`. qli:
  `qli-Client` against the qubic.li pool, always PPS. The agent downloads both
  miner packages at container start (see "Rig runtime"), so switching pools
  is a restart of the miner inside the running rental — the rental stays up. A cheap rental that isn't
  yielding on one pool gets tried on the other before we release it. Payout
  mode (jetski solo vs PPLNS) is not a design driver for now.
- **Rig runtime: our own Docker image on Clore, no HiveOS.** The user is on
  the HiveOS free plan, which has no API access (Hiveon: "API Access will now
  be limited to paid farms only"). Rather than pay for HiveOS, clorqub ships
  its own small image (`clorqub-miner`) with an in-container agent, and
  controls it over SSH. Miners are **not baked in**: like HiveOS, the agent
  downloads the pools' HiveOS custom-miner packages from their `latest`
  release URLs on the fly, because both projects release often and
  third-party Docker images (e.g. qli's) fall behind. Benefits: no HiveOS API, worker cap or
  bill; stats come straight from the miners' own files; boot, image pull and
  trainer download times are measured directly. Lost: HiveOS dashboards and
  GPU overclocking (not needed for v1).
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
  variables; the systemd units use `EnvironmentFile=`. Currently holds
  `CLORE_API_KEY`.

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
  listings**; every server has at least one GPU, so CPU is always a side
  benefit of a GPU rental.

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
  config.toml          # max_rentals / burst settings, max_premium, budget cap,
                       #   intervals, country_blocklist, thresholds, image tag
  image/               # clorqub-miner Docker image (no miner binaries)
    Dockerfile         #   two tags: cpu (small base) and cuda (NVIDIA)
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
                       #   set-budget / set-max-rentals / servers /
                       #   server <id> / hardware / blacklist add|rm|list /
                       #   switch-pool / release
    tui.py             # Textual viewer (Phase 5)
```

### Rig runtime: clorqub-miner image

One small image, miners downloaded at start, one miner running at a time.

- **Contents:** `agent.py`, `qubic_parse.py`, `openssh-server`, `curl`,
  `jq`, `tar`. **No miner binaries.** Two tags: `cpu` (small base, fast
  pull) and `cuda` (NVIDIA runtime base, for GPU mining). `scorer.py` picks
  the tag per server: `cpu` unless GPU mining is expected to pay for the
  larger pull.
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
- **Order:** `create_order` with `image=<registry>/clorqub-miner:<tag>`,
  `ports={"22":"tcp"}`, `ssh_key=<clorqub public key>`, and `env`:
  `POOL=jetski|qli`, `QUBIC_WALLET`, `ALIAS=clorqub-<server_id>-<order_id>`,
  `CPU_THREADS` (from `specs.cpus`, minus 1), `GPU=on|off`, `JETSKI_MODE`,
  and the package URLs. The wallet is a public address, so passing it in env
  is fine; no secret goes into env.
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
- **Image registry:** Docker Hub or GHCR (public image; no secrets inside).
  Image pull time counts against the 10-minute free window, so the `cpu` tag
  is kept small.

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
  drill-down, hardware reference, decision log, command entry. Runs locally
  or over SSH.
- **Web view** (later): FastAPI reading the same file, bound to localhost or
  behind auth.
- **Discord bot** (future phase, user already has a server): posts `events`
  and accepts commands from allowlisted Discord user ids in designated
  channels. Same `commands` / `events` path as every other client, so no
  daemon changes are needed to add it. The bot token is an env var.

### Clore billing

- **Prices:** the API's `price.usd.on_demand_usd` is **USD per day**. Spot
  bids are per-currency (`spot_marketplace` gives `min_pricing` and competing
  `offers`). Lowering a spot bid is limited to once per 600 s.
- **Creation fee:** Clore charges a creation fee once a rental passes 10
  minutes: **$0.01 for spot, $0.10 for on-demand**. A rental cancelled inside
  the first 10 minutes is free (confirm the exact rule in Phase 0). Assumed
  not charged again when a spot rental is outbid and re-won. The fees are in
  config (`creation_fee_spot`, `creation_fee_on_demand`), not hardcoded.
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
  Every timestamp in the boot timeline is stored, so slow pulls and downloads
  are visible per server and per country.
- **Spend tracking** uses what Clore actually charged (fees + rental), not
  price x hours. Clore prices in CLORE/BTC, so USD cost moves with the
  exchange rate; store both the native amount and the USD rate at the time
  (`currency_rates_in_usd` from the API).
- Every order is placed with a **maximum duration** (where the API allows; the
  listing's `mrl` is the host's cap), so a rental can't bill forever if
  everything else fails.

### Network / region filter

Servers in **Russia and Ukraine** often have major bandwidth problems
(slow trainer downloads, dropped pool connections). Together they are 39% of
available servers.

- `country_blocklist` in config (country = `specs.net.cc`), default
  `["RU", "UA"]`: these servers are dropped before scoring, like blacklisted
  servers. Editable via config or a command.
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

### Hardware reference

Every server's CPU and GPU details are kept in SQLite for reference, whether
or not we rented it.

- `servers`: clore `server_id`, `owner` (hoster id), country, first/last
  seen, latest listing.
- `server_specs`: one row per **distinct spec set** seen for a server
  (valid_from / valid_to), so a host swapping hardware or changing the threads
  offered is visible: CPU model, `cpus` cores/threads offered, RAM, disk +
  speed, motherboard, PCIe rev/width, GPU models (`gpu_array`), GPU count,
  VRAM, CUDA, power limits, listed net down/up + latest speed-test time.
- `server_prices`: price history (on-demand and spot min, per currency, plus
  USD/day), sampled each poll when changed.
- `rig_reports`: what the rig actually reported at boot (CPU model, `nproc`,
  CPU flags such as AVX2/AVX-512, `nvidia-smi` GPUs, driver/CUDA). Compared
  with the listing for the spec-mismatch check.
- `cpu_models` / `gpu_models`: normalized model names (e.g. "Ryzen 9
  9950X3D", "RTX 3090 Ti") with the perf prior: it/s per thread (CPU) or per
  card (GPU), per pool and trainer version, its source (benchmark, measured),
  sample count, and last update. Seeded in Phase 1, learned from rentals.
- `cli.py hardware` lists models with their measured it/s and how many
  servers/rentals back each number; `cli.py server <id>` shows spec history.

### Per-server tracking and blacklist

Every rental is keyed by its **Clore server id**, so we can see how each
physical server does over time, across rentals.

SQLite tables (besides the hardware reference above):

- `rentals`: clore order id, `server_id`, image tag, start/end, price,
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
  `qubic_per_share`.
- `prices`: QUBIC/USD and CLORE/USD samples, source, time.
- `blacklist`: `server_id` or `owner` (hoster), reason, source (auto/manual),
  added at, optional expiry.
- `commands`, `events`, `decisions`: see "Process model".

Use:

- **Scoring**: blacklisted servers/hosters and blocked countries are dropped
  before ranking. For other servers, blend that server's measured it/s with
  the hardware prior, weighted by how much rented time we have on it. A
  known-good server can then beat an untested one with the same specs, and
  the reverse.
- **Exploration**: with only 2 slots, exploration doesn't get a dedicated slot.
  Instead, an untested server whose prior-based score is close to the best
  (within a configurable margin) may take a slot, at most one at a time, so a
  wrong prior for a hardware model gets corrected.
- **Auto-blacklist** (thresholds in config, every trigger logged with its data):
  - measured it/s < X% of the hardware prior over >= N hours of rentals;
  - failed the early boot check;
  - repeated stalls (the adaptive stall detector) or spot interruptions;
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
- **per worker**: one row per worker alias (`clorqub-<server>-<order>`), with
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
     - never over the daily budget
     -> create_order with the clorqub-miner image, POOL=jetski
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
- **Image hygiene**: no miner binaries in the image; no secrets in the
  image or in order env; the rig SSH key is dedicated to clorqub.
- Secrets (`CLORE_API_KEY`, later the Discord bot token) only in
  `~/.config/clorqub/env` (mode 600) / env vars, never in config, SQLite, logs
  or the image.

## Phases

| Phase | Deliverable | Depends on |
|-------|-------------|------------|
| 0 | Done so far: Clore API auth/rate limits/endpoints, marketplace field meanings (cross-checked with web UI), price units, spot data, region/network data, HiveOS dropped, miner packages (URLs, `.hash` verification, config formats, jetski local API). **Remaining:** (1) Docker order specifics — `ssh_key` / `command` / max-duration fields, time from order to SSH, whether hosts allow mining in Docker orders; (2) creation-fee rule and actual-charge reporting, via one cheap test rental (with user approval); (3) both miners running outside HiveOS in a plain container, and a real jetski `/summary` response; (4) jetski and qubic.li per-worker stats and payout APIs; (5) QUBIC/USD price source; (6) Qubic idle-phase schedule | — |
| 1 | Read-only scanner + scorer: marketplace -> `servers` / `server_specs` / `server_prices`; rank by QUBIC per dollar with premium ceiling and country filter, show effective cost per QUBIC vs market, print table. Seed `cpu_models` / `gpu_models` (it/s per thread / per card) from published benchmarks, the local rig, and/or a few short test rentals; seed QUBIC/share from current pool payouts | 0 |
| 2 | `qubic_parse.py` (copied parsers for both miners + tests against real `stats.<epoch>.lock`/log samples) + `clorqub-miner` image (cpu + cuda tags, agent, both miners, `.lock` mirror) + SQLite schema incl. `commands`/`events` + collector + single-rental path: rent one server, early boot check, confirm shares — on each pool, with a switch between them that loses no data | 0 |
| 3 | Share summary windows (15m/1h/2h/4h/24h/epoch; fleet / worker / server, split by pool run) + per-server history + learned perf model + payouts + blacklist (manual + auto rules, `servers`/`server <id>`/`hardware` reports) | 2 |
| 4 | Full loop: spend policy (2-rental limit, burst, premium ceiling, budget) + exploration + pool switching + rotation with hysteresis + watchdog | 1, 3 |
| 5 | Textual TUI client (reads SQLite, sends `commands`) | 4 |
| 6 | Later: web view (FastAPI, localhost/auth); Discord bot (posts `events`, accepts allowlisted `commands`) | 4 |

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
- Exact creation-fee rule ($0.01 spot / $0.10 on-demand): is the fee waived
  only if the order is cancelled before 10 minutes, and is the 10 minutes from
  order creation or from the server starting? Why is the web price ~2–7% above
  API USD/day ÷ 24 — a fee we should include in cost?
- Spot: how bids are won and lost (outbid behavior, minimum increment), and
  whether a spot rental that is outbid is paused or ended.
- Miner packages run outside HiveOS: confirm both binaries run in a plain
  container (any missing libraries, GPU runtime needs), and what qli's
  `accessToken` vs `qubicAddress` means for pool-side stats.
- Does jetski expose per-worker stats and payout APIs? If not, rig stats +
  wallet payouts only.
- Does qubic.li expose a per-worker stats API suitable for yield measurement?
- QUBIC/USD price source (exchange or aggregator API); CLORE/USD comes from
  `spot_marketplace` `currency_rates_in_usd`.
- GPU vs. CPU share yield on each pool: decides when the `cuda` image and GPU
  mining are worth it.
- jetski `127.0.0.1:17899/summary`: capture a real response to fix the
  fields the agent reads (does it include share counts, or only versions and
  hashrate?).
- Confirm qli's `.lock` deletion behavior also happens inside the container.
- Qubic idle phases exist (qli has `Idling` settings): on what schedule, and
  do they need a "no shares expected" window in stall detection?
