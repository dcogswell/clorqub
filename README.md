# clorqub

Rent Clore GPU rigs, mine Qubic on the jetski or qubic.li pools, and keep
the ones that yield. Read the full design in `PLAN.md`.

Layout:

```
rig/            code that runs inside a rental (fetched by the bootstrap)
  bootstrap.sh    order `command`: lock down image, key-only sshd, exec agent
  agent.py        in-rental supervisor: fetch+verify miner pkgs, run/switch
                  miner, status.json, stats mirroring, boot timeline
  qubic_parse.py  parsers copied verbatim from mining_optimizer.py
  SHA256SUMS      integrity file for agent.py + qubic_parse.py
scripts/update_sums.sh  regenerate rig/SHA256SUMS (run before every commit
                        that touches those files)
market.py       read-only Clore marketplace scanner + conservative scorer
cli.py          read-only scan CLI
tui_data.py     read-only SQLite queries for the TUI
tui.py          Textual TUI over the scanner db (+ guarded rent flow)
requirements-tui.txt  TUI dependency (textual>=0.89,<9)
tests/          stdlib unittests (python3 -m unittest discover -s tests)
```

## CLI: `cli.py scan`

Ranks marketplace listings by provisional sols-per-dollar. Read-only: it
only ever fetches the marketplace and records it locally -- it cannot
rent, cancel, or bid.

```bash
python3 cli.py scan --snapshot snapshot.json [--db PATH] [--top N]
python3 cli.py scan --live [--env-file PATH] [--db PATH] [--top N]
```

- `--snapshot PATH` -- a saved Clore marketplace JSON response. It is a
  stored file, **not** fresh live availability; listings may be rented or
  repriced since it was captured. The banner shows the file path and the
  import time separately.
- `--live` -- fetches `GET /v1/marketplace` once. Needs `CLORE_API_KEY`
  in the process environment, or failing that in `--env-file`
  (default `~/.config/clorqub/env`). A key already in the environment
  wins, so an inaccessible env file never blocks a supplied key.
- `--db PATH` -- SQLite store for snapshots/server history
  (default `~/.local/share/clorqub/clorqub.sqlite3`). `:memory:` works.
- `--top N` -- rows to print (default 25).
- `--watch --interval SECS` -- opt-in rescan loop, `--live` only
  (finite, >= 15, default 300 = 5 min). Still read-only: it only
  refetches the marketplace; no orders are placed. Prints one status
  line per scan; each successful scan appends to the local history, so
  the db grows (no retention/pruning yet). Stop with Ctrl-C; it is
  never auto-started.

## Collector service: `collector.py`

Same watch loop, but inside a **transient systemd --user service**
(`systemd-run`, unit `cq-scanner.service`) so it survives your terminal.
Not installed, not enabled at boot; depending on your user manager /
linger it may end at logout. Read-only scans only, no orders; the
service reads the protected env file itself -- no credential values are
passed on any command line. History grows; there is no pruning.

```bash
python3 collector.py start               # starts in background; default 300s
.venv/bin/python tui.py                  # dashboard in this same terminal
```

Collector controls (run from a shell):

```bash
python3 collector.py status              # load/active state
python3 collector.py logs                # last 50 journal lines
python3 collector.py stop                # stops collection, not the TUI
```

To choose a faster interval, stop the collector first, then use
`python3 collector.py start --interval 60`.

Then the TUI only needs one terminal. Equivalent foreground loop:
`python3 cli.py scan --live --watch` (default 300s). A watcher already
running with an explicit `--interval 60` keeps that value; restart it
to pick up a different interval. To enable spot-floor observations on a
running collector: `collector.py stop` then
`collector.py start --spot-limit 3`.

### What each live scan costs and records

Every live scan (one-shot, watch cycle, or collector) makes **two GETs**:
`marketplace` plus `my_orders` (order inventory -- observe-only; this is
how manually created web-UI rentals show up). `--spot-limit N` adds at
most N `spot_marketplace` requests, spaced ~1.1 s apart; default 0 adds
none. A failed inventory poll keeps the last good snapshot and skips the
spot batch, so an error never triggers extra requests or masquerades as
an empty fleet.

- **SpotFlr** column: observed fee-inclusive spot floor
  (USD-Blockchain only, 1.25% fee assumed); `*` = offers present,
  `-` = not collected/stale. Indicative only -- not a winning-bid price
  and no execution guarantee. Ranking stays on-demand; there is no
  spot sols/$ sorting.
- **Orders** (CLI table / TUI Orders tab): order id, server id,
  spot/on-demand, online-reported, native-currency price/day and
  cumulative reported spend. The TUI tab lists every order ever seen
  in local snapshots, marked active or ended -- not just the latest
  poll. Observe-only; reported spend is not
  epoch-verified and a creation-fee quote is not proof of charge.
  Manual-order budget reconciliation and epoch controls are planned
  (see PLAN.md), not implemented.

### What the ranking actually is

- **Pool:** jetski only. qubic.li is not scored.
- **Scored set:** single-GPU listings of RTX 3070, RTX 3080 Ti, or
  RTX 3090 Ti whose listed CPU matches the benchmarked reference models
  (Ryzen 9 7945HX / 7950X). Every other listing is retained unscored in
  SQLite but not shown in the ranked output -- there is deliberately no
  yield prior for other CPU profiles.
- **Yield anchors:** two user-supplied end-to-end observations:
  `~160 sols/h` (approximate) for RTX 3070 and `>350 sols/h`
  (lower bound) for RTX 3080 Ti / 3090 Ti. The `~`/`>` column markers
  carry that provenance. These describe the **reference rig** the
  observations came from -- they are not a guaranteed floor or expected
  rate for any candidate listing.
- **Price:** `$/day` includes an assumed 5% renter fee based on prior
  observation; it is not freshly fetched from the API. The order
  creation fee and boot opportunity cost are omitted.
- **sols/$** is a provisional sort key -- this is a shortlist, not a
  rental approval.

### Limitations (read before trusting a row)

- The reference rig's **CPU thread allocation, epoch, and trainer
  version are unknown**, so equal CPU allocations are **not** assumed
  between the reference rig and a listed rig.
- sols/h is a **whole-rig** figure: CPU and GPU it/s are **not**
  additive and are not broken out separately in the estimate.
- No conversion to QUBIC yet; sols/$ is not revenue per dollar.
- CUDA >= 12.0, single advertised GPU, non-blocklisted country, and a
  supported on-demand USD price are required for a listing to score.
- Spot prices are never scored: the API's USD spot field is a zero
  placeholder, not a quote.

## TUI: `tui.py`

Read-only Textual viewer over the scanner's SQLite db. It shows the
latest imported snapshot only -- Candidates (ranked shortlist),
Marketplace (all listings incl. unscored/rented, with CPU + OD price
visible at 80 columns, reason column to the right), Benchmarks (stored
anchors + context observations), Orders (observe-only inventory, all
orders ever seen -- active and ended), and Fleet (active enrolled rigs
only: health, independent CPU/GPU it/s, share counters, burn $/hr and
accrued spend; a rig whose order vanishes from a fresh inventory is
hidden, and marked `missing` by telemetry). Enter on a row opens details. The dashboard itself never
calls the API; the only non-read-only paths are the guarded `b`
rental flow and the one-shot `l` log fetch described below.

The scanner populates the db first -- the TUI only reads what
`cli.py scan` wrote:

```bash
# one-time setup (no system pip changes)
python3 -m venv .venv
.venv/bin/pip install -r requirements-tui.txt

# populate the db (independent of the TUI)
python3 cli.py scan --live
# or: python3 cli.py scan --snapshot snapshot.json

# view it
.venv/bin/python tui.py --db ~/.local/share/clorqub/clorqub.sqlite3 --refresh 5
```

Keys: `/` search, `a` available-only, `s` toggle sols/$ vs $/day sort,
`c` reset filters, `r` reload db (the db file, not the API), `l` miner
log tail on a Fleet or Orders row (ended rigs show the archived copy),
`?` help, `q` quit, Enter detail, Escape/q close detail, `j` raw JSON
in detail.

`b` on a Candidates/Marketplace row opens a setup sheet (hours --
with a **Max** button that fills in the listing's `mrl` host maximum,
shown in the hint, disabled when the listing has no usable `mrl` --
optional cost ceiling (blank means you confirm the exact quoted
bound), pool, optional threads, and an off-by-default **auto-renew**
checkbox), then runs the same guarded flow as
`rent.py create --live` in-process: fresh price/fee/order GETs, an
in-TUI review of the exact bounded quote, and your explicit
**Confirm & Rent** click. All paid-path rules are identical to the
CLI -- fresh re-verification under the operator lock, one
`create_order` POST never retried, auto-cancel on a bad receipt or
provisioning failure, ambiguous outcomes stay `unknown` until
`rent.py reconcile`. Escape/Cancel at any review step places nothing;
a second `b` is refused while a rental is in progress. Run in a shell,
`rent.py create --live` still asks you to type `rent <id>` (or
`--confirm-server <id>` non-interactively) -- that stays true.

`l` on a Fleet row performs ONE explicit read-only SSH fetch: the
tail of `/clorqub/logs/<pool>.log` (pool taken from the last sanitized
status, falling back to the enrolled pool; unknown pool refuses).
Refresh refetches; there is no polling, no writes to the rig, and no
API call. Escape/Close dismisses the view. If the live fetch fails
(e.g. the rig is gone), the locally archived copy is shown instead
when one exists.

While `scan --watch` (or the collector) runs, the same 30 s telemetry
tick that reads `status.json` also appends the new bytes of each rig's
`/clorqub/logs/<pool>.log` into `rig_log_chunks` in the db, tracking a
per-rig/pool byte cursor (log shrink/rotation re-reads the tail). The
archive survives rig death and web-UI cancellation; it is what `l`
falls back to. A managed rig that disappears from the latest good
order inventory (fresh, <15 min) while its SSH is failing is marked
`missing` and skipped, instead of being polled forever.

Opening a Fleet row shows the rig detail with three actions besides
Close/Copy: **Cancel order** (one `cancel_order` POST, never retried)
and **Auto-renew** (enabled only while the order is `pending`/`ready`).
Auto-renew calls Clore's `set_order_auto_renew`: enabling keeps
re-billing each term until you cancel and *requires* a max $/day cap
(`auto_renew_max_price`, prefilled at 1.25x the last observed native
price/day -- the web UI's own default) so renewal stops automatically
if the host raises the daily price above it; disabling lets the order
expire at its current term end. Enabling is a *request*, not a
guarantee: Clore re-checks at every term boundary and the order ends
with the current term if the renewal is declined -- host repriced
above the cap, host disabled renewals, the server is no longer listed
in your currency, or your balance can't cover the next term's first
hour. The decline reason shows in the web UI order card, not the API.
Clore only allows the call on active on-demand orders --
spot and ended orders are refused, and a `renewal_in_progress` error
in the last 60 s of a term just means Clore is already renewing it.
One POST, never retried; a failure tells you to check the web UI.
Observe-only rigs can never be renewed or cancelled from here.

If the db doesn't exist yet (no scan has run), the TUI shows the error
and stays open; run a scan first.

## Operator rentals: `rent.py`

`rent.py` is the only component that can spend money. Everything else
stays read-only.

```bash
python3 rent.py create --server 111997 --hours 1 --max-cost-usd 0.50 \
    --pool jetski [--cpu-threads 8] [--auto-renew]
python3 rent.py create ... --live --confirm-server 111997
python3 rent.py list
python3 rent.py cancel --order 12345 --live
python3 rent.py enroll --order 12345 --host HOST --port 2222
python3 rent.py reconcile [--clear-empty ATTEMPT_ID]
```

Policy (hard-coded, not configurable): on-demand USD-Blockchain only,
1-24 hours, at most 2 current orders including ones made in the Clore
web UI, no spot orders (the API ignores `rental_hours` for
spot). Renewal is off unless you pass `--auto-renew` (or tick the TUI
checkbox): it asks Clore to re-bill each `hours` term until you cancel,
and the quoted cost bound covers ONE term only -- the confirmation
screen says so loudly. If the API doesn't echo `auto_renew: true` on
the receipt the order is treated as a term mismatch and auto-cancelled. `create` is a dry run without `--live`; `--live` prints the
exact order and requires typing `rent <id>` interactively, or the
non-interactive `--confirm-server <id>`. One `create_order` POST is
made -- never retried; an ambiguous outcome stays an `unknown`
attempt in the db and blocks new spend until `rent.py reconcile`
(or `--clear-empty` after interactive confirmation) resolves it.

The receipt is verified after create: `rental_hours` must equal the
requested term and the native `rental_end` must equal `ct + hours x
3600` exactly (renewal is per-term; the end bound still applies). With
`--auto-renew` the receipt must echo `auto_renew: true`. Without it,
`auto_renew` omitted is documented as disabled (the API drops the key
when renewal was never enabled), but enabled, null, or non-boolean
`auto_renew`/`spot`, or a missing/different `rental_end`, fails
closed and that exact new order is auto-cancelled instead of
provisioned. The same auto-cancel happens if SSH
provisioning fails within 180 s. This fails closed if the API ignores
the duration params; it cannot stop billing by itself.

Requires a dedicated ssh keypair (default
`~/.config/clorqub/id_ed25519[.pub]` -- create it yourself, it is never
generated for you) and `CLORE_API_KEY`, `JETSKI_WALLET`,
`QLI_ACCESS_TOKEN` in the env or `--env-file`. Credentials are pushed
to the rig over SSH stdin as `/clorqub/credentials.json` (mode 600) --
never in order env, args, or logs. `enroll` records an existing
account order for read-only observation only (no deploy, no tokens).
Rental/attempt records live in the same sqlite db (fleet_*,
rental_attempts tables). Current lifecycle state is updated; telemetry
observations are append-only. Neither is pruned.
Order creation is guarded,
on-demand-only, and every live order needs explicit confirmation --
there is no autonomous selection or epoch budget enforcement yet.
An `enroll`ed (observe-only) rig must already run the clorqub agent;
enroll never bootstraps or deploys to manual hosts.

While `cli.py scan --watch` (or the collector) runs, enrolled rigs are
also SSH-read every 30 s (agent `status.json`, sanitized) on a
separate tick -- this never adds Clore API calls; the marketplace
poll stays at the configured interval (5 min default). Watch the
Fleet tab in the TUI for per-rig health and independent CPU/GPU it/s.
Already-running watchers need a restart to pick up the telemetry
tick and new table layout.

## Server blacklist

`blacklist.py` tracks servers to avoid, with reason/evidence and expiry.
Automatic entries (written by `experiment.py`) expire after 3 days; manual
entries never expire:

```bash
python3 blacklist.py list [--all]                 # active (or all) entries
python3 blacklist.py add --server ID --reason TEXT # manual, no expiry
python3 blacklist.py rm --server ID               # clear active entries
```

All commands take `--db PATH`. The experiment runner writes auto entries for
confirmed fault/unreachable cancels, failed provisioning, >40% failed rig
reads over 30 min (20+ samples), and CPU/thread mismatch vs the listing.
Share yield, dry trees, low it/s and slow first shares never trigger a
block -- throughput is priced by the value model, not blacklisted. Blocked servers are
excluded from automatic fills/probes/re-rents and TUI Candidates; manual
`rent.py create` and the TUI review show a warning but still allow the
rental. An already-running experiment process must be restarted to pick up
the rules (Ctrl+C, then the same `run` command -- rentals are unaffected).

## Rig side


Rentals run `cloreai/hiveos:0.4` with the order `command` replaced by
`rig/bootstrap.sh <commit>`: it locks the image's `user` account,
enforces key-only sshd, fetches `agent.py`/`qubic_parse.py` at that
commit, verifies them against `rig/SHA256SUMS`, and execs the agent.

`agent.py` env: `POOL` (jetski|qli), `JETSKI_MODE` (pplns|solo),
`ALIAS` (default `cq-<hostname>`), `CPU_THREADS`, `GPU` (on|off),
`PKG_URL_*` overrides. It polls `/clorqub/control.json` for
`agent.py switch <pool>`, writes `/clorqub/status.json` every 5 s
(including per-device `its` and `boot` timeline), mirrors `stats.*.lock`
files, and persists the active pool in `/clorqub/state.json` so
restarts don't revert a switch. Credentials arrive over SSH as
`/clorqub/credentials.json` -- never in the order env.

SSH'd into a rig, the stock HiveOS `miner` command works: the agent runs
the miner under a pty, keeps the raw colorized stream in
`/clorqub/logs/<pool>.raw.log`, and maintains a detached `miner` screen
session tailing it plus a `/run/hive/miner.1` symlink. `miner` attaches
to that session (live color log; Ctrl+A d detaches, Ctrl+C only stops
the view -- not the miner) and `miner log` tails the last 80 lines like
stock HiveOS. `miner start/stop/config` stay non-functional; the agent
owns the miner lifecycle.

## Tests

```bash
python3 -m unittest discover -s tests
scripts/update_sums.sh   # after editing rig/agent.py or rig/qubic_parse.py
```
