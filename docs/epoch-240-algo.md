# Epoch 240 algorithm — what we know

Everything below is reconstructed from live telemetry, miner logs, and pool
snapshots in `clorqub.sqlite3` (Nov 18, 2026). Facts are marked **[observed]**;
interpretation is marked **[inferred]**.

## The pipeline

```
jetski pool coordinator
        │  "ant trainer tunnel" per device type
        ▼
QUBJETSKI miner v4.5  ──►  per-device TRAINER (the algo)
        │                    AVX2 / AVX512 (CPU) · CUDA (GPU)
        │  hash walks over maps/keys inside a population of 1024
        ▼
   shares (partial-difficulty results) ──► pool counts them (PPLNS)
   solutions (full-difficulty results) ──► solo-class payout
```

- **Miner binary:** `QUBJETSKI v4.5` (same binary across 239→240 —
  `package.sha256 e919f39d…` unchanged). **[observed]**
- **The algo is not the binary — it's the trainer bundle.** Each device type
  downloads a versioned *trainer*: `Trainer Updated: AVX2 v240.0`,
  `Trainer Updated: CUDA v240.0`. The version tracks the epoch number. 
  **[observed]**
- **Trainers also patch mid-epoch.** Epoch 239 ran `239.14` → `239.15.2` →
  `239.15.3` → `239.15.4` → `239.15.5` → `239.15.6` across ~36 h — five
  minor updates inside one epoch. Expect `240.x` bumps; measurements can go
  stale *intra*-epoch, not just cross-epoch. **[observed]**

## The work itself — training a Bitcoin price predictor

**Epoch 240's task (per operator): the pool trains a Qubic "Aigarth" neural
network — a Bitcoin price predictor fed realtime BTC market data — on a
population of 1024 neurons.** The mining IS the training; that's the
"useful proof of work" pitch — your hashpower evolves better predictors and
the pool monetizes the result.

That makes the observed log mechanics literal:

- `ANT population-1024 sequence screen` — a **1024-neuron population** being
  evaluated on *sequences*: the incoming realtime BTC price feed sliced into
  windows and screened against each neuron. **`output 443`** is a neuron's
  prediction output on its sequence. **[observed + operator context]**
- `ANT parent installed score=4129 shift=0 depth=8 mode=2` — evolutionary
  search: the coordinator broadcasts the current-best **neuron parent**
  (score = its prediction accuracy); each worker searches variations around
  it (shift/depth/mode = where in weight-space to mutate). **[observed +
  inferred]**
- `ANT refresh requested: parent-change hint` / `active work cancelled` —
  **the model you're searching around is live.** When the population breeds
  a better parent, in-flight evaluations re-center on it. **[observed]**
- `designed finder: kinds alt balt salt, maps/keys, walks` — structured
  weight-space search plans the GPU executes; `hashes/walks/kept` counts the
  evaluations. **[observed + inferred]**

So a "share" = a verified evaluation/improvement the pool can credit, and a
"solution" = a full-difficulty find (a genuinely better network or a
block-winning result). it/s = evaluation throughput on the neuron search.

**This explains tree draws perfectly:** you aren't mining a static
difficulty, you're searching around a *living model* whose parents churn as
the population learns — your rig can land a lineage that's about to bloom or
one that's already stale. A fast rig can draw a dead parentage
(`probe_lost`); a mediocre one can hit the vein.

## What actually changed in 240

| signal | epoch 239 | epoch 240 | meaning |
|---|---|---|---|
| trainer | 239.14→239.15.6 | **240.0** | new task bundle **[observed]** |
| share rate per it/s | ~0.5–0.7 shares/h | **~0.09–0.2 shares/h** | shares ~4–6× scarcer per unit work **[observed]** |
| GPU it/s (5070 Ti) | ~380–460 | ~408–470 | newer GPUs still dominate **[observed]** |
| GPU it/s (2070-class) | ~100 | ~100 | older flat **[observed]** |
| CPU it/s | tens | 5–26 | minor contributor either way **[observed]** |
| pool `trainers` rates | — | AVX2 268 · AVX512 2850 · CUDA 3590 | pool-reported per-device work rates **[observed]** |

**[inferred]** Reading it together: v240's share difficulty is ~5× higher —
the predictor task's creditable-evaluation bar moved with the trainer — which
economically means **fewer, bigger shares.** If the pool prices counted
shares to compensate (the `usd_per_counted` spike is consistent with that,
though inflated by tiny early denominators), the value-per-it/s may be
unchanged. That's the thing to verify at re-analysis:
**watch `usd_per_counted` converge and compare per-it/s earnings against the
239 baseline, not raw share counts.**

## Why the runner's assumptions broke

1. **it/s is hardware-bound and carries across epochs** — a 5070 Ti does
   ~410 it/s under 239 *and* 240 (that's why `server_prior`/`meas it/s`
   stays meaningful). **[observed + inferred]**
2. **it/s → shares → $ does *not* carry** — it's set by the trainer bundle's
   difficulty and the pool's counted-share value, both epoch-scoped.
   That's why `cross_epoch` flags exist and why epoch-239 USD columns are
   meaningless until the value model re-calibrates. **[inferred]**
3. **Fixed-time yield gates misfire under scarcity** — a healthy rig at
   ~0.1 shares/h·it/s shows zero shares in an 8-min window ~40% of the
   time. That produced the 13-dead-in-50-min churn (and ~53% of epoch-239
   spend going to creation fees). The Poisson `kill_lambda` gates now wait
   for expected-share mass instead of a clock. **[observed]**

## Cheat sheet for this epoch

- **Fast hardware:** 5070 Ti (~410–470 it/s) is the verified class winner;
  5080 (~260 and ramping) and 3080 Ti (~322) trail. 5090's 896 is a
  *cross-epoch* number — unverified under 240.
- **Prices that make sense:** 5070 Ti at $0.21–0.26/h dominates on
  meas/$; RTX 3060 at $0.05/h is the budget floor.
- **Expect ~10–20 shares/h per ~400 it/s rig** — 0-share windows of
  15–20 min are *normal*, not dry. Judge nothing on raw share counts.
- **Expect trainer patches** (`240.x`): re-check `trainer.version` in rig
  telemetry if share rates shift mid-epoch.
- **Payout math:** counted shares × ~624 QUBIC/unit (238's rate) × QUBIC
  price. The pool's `usd_per_counted`/`est_gains` overstates early —
  trust it only after counted shares reach the thousands.
