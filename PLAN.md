# clorqub — Qubic shares-per-cent optimizer for Clore.ai

## Goal

Maximize Qubic mining shares per dollar of Clore.ai rental spend.

```text
score(server) = expected_shares_per_hour(gpu_model) / price_usd_per_hour
```

Poll the marketplace, rank by score, rent the best rigs within budget, deploy
the miner, measure actual shares, and rotate out underperformers.

## Assumptions (verify before building)

- Miner: `qli` client (qubic.li pool) — most common for Qubic. Configurable so
  other pool clients can be swapped in.
- SDK: official `clore-ai` Python package (`pip install clore-ai`), auth via
  `CLORE_API_KEY` env var.
- Share measurement source: pool stats API and/or the qli client's local
  stats endpoint. Exact endpoints TBD in Phase 0.
- Deployment: rented Clore servers run a Docker image or custom start command
  containing the miner + config.

## Architecture

Python daemon + CLI, SQLite for state. No web UI in v1 (see Phases).

```text
clorqub/
  config.toml          # budget cap, poll intervals, miner config, thresholds
  clorqub/
    market.py          # marketplace polling via clore-ai SDK
    perf.py            # shares/hr-per-GPU model: seed table + measured data
    scorer.py          # shares-per-cent ranking
    rentals.py         # order lifecycle: rent, extend, release
    deploy.py          # miner deployment to rented servers
    stats.py           # collect actual shares from pool/miner
    rotate.py          # drop/replace logic
    store.py           # SQLite: rentals, measurements, spend, decisions log
    daemon.py          # main loop
    cli.py             # status / report / dry-run / pause / set-budget
```

### Core loop

```text
every N minutes:
  1. snapshot marketplace (gpu model, price/hr, availability)
  2. for each active rental: pull share stats, update perf model
  3. score all available servers
  4. rent top-scoring servers while under budget cap
  5. release rentals whose measured score < threshold * market best
  6. log every decision with its inputs (scores, prices) to SQLite
```

## Safety rails

- **Dry-run default**: prints what it would rent/release; `--live` required to
  spend money.
- **Budget cap**: hard `$`/day limit in config; daemon refuses to exceed it.
- **Max concurrent rentals** cap.
- **Grace period**: don't release a rig before Clore's minimum billing period /
  warm-up time for share measurement.
- **Decision log**: every action + rationale persisted, so behavior is auditable.
- API key only via env var, never in config or logs.

## Phases

| Phase | Deliverable | Depends on |
|-------|-------------|------------|
| 0 | Verify clore-ai SDK surface (order create/release, marketplace fields, spot vs fixed pricing) + qli deployment method + share stats source | — |
| 1 | Read-only scanner + scorer: rank marketplace by shares-per-cent, print table | 0 |
| 2 | Single-rental path: rent one server, deploy miner, confirm shares | 0 |
| 3 | Stats collection + learned perf model (measured shares/hr per GPU) | 2 |
| 4 | Full loop: budget-capped auto-rent + rotation | 1, 3 |
| 5 | Optional: read-only web dashboard (FastAPI + single HTML page reading SQLite, localhost only) | 4 |

Phase 1 is usable standalone — it answers "which Clore rig gives the most
shares per cent right now" without spending anything.

## Open questions to resolve in Phase 0

- Exact `clore-ai` order API (create/cancel/extend, min durations, deposit rules).
- How qli is deployed on Clore (prebuilt image vs. custom command) and how its
  auth token is injected.
- Where shares are observed: qubic.li pool API, or the client's local stats.
- Whether spot rentals are worth including (cheaper, but can be interrupted —
  kills share continuity within an epoch).

## Models / cost

Fusion recommended: frontier lead (Opus/Fable/Astra) plans + reviews, cheap
sidekick (SWE-2 or Luna) implements. Select with `/fusion`.
