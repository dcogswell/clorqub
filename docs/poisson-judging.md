# Evidence-based kill gates (Poisson judging)

Why dry/low-yield verdicts stopped running on a fixed clock, what the
runner does instead, and how to tune it. Implementation:
`experiment.py` `decide()` (~lines 2341–2440, 2654–2679); config keys
`kill_lambda` / `kill_lambda_free` (defaults 3.0 / 2.2, live-tunable
via `POST /api/config` or the Settings tab — no restart needed).

## The problem

Share production is a Poisson process whose rate is set by the pool's
share difficulty — a number the runner doesn't control and that moved
~4–6× between epochs 239 and 240. Fixed kill windows calibrated on
one difficulty become coin flips on another:

- a healthy ~100 it/s rig expecting ~12 shares/h shows **zero shares
  in an 8-minute window ~30–45% of the time**;
- each false kill costs the creation fee ($0.10 on-demand) + a probe
  to replace the slot + a burst slot — and the killed rig's history
  now records a `low_yield` verdict it didn't deserve;
- epoch 239's accounting showed the result: **$17.73 of the $33.71
  experiment spend was creation fees** — churn tax, not compute.

The old gates had the right *questions* ("produced yet?" "below
floor?") but a fixed-clock idea of "long enough to know." Under
scarce shares, "long enough" is longer — and it's computable.

## The rule

For any rig, the value model predicts a share rate:

```
predicted_h = S × (its / its_ref)^β        [shares per hour]
λ = predicted_h × hours_since_hashing      [expected share mass]
```

Kill a "dry" verdict only when **λ ≥ kill_lambda**. A healthy rig
produces zero shares in that window with probability ≈ e^−λ:

| λ | P(0 shares \| healthy) |
|---|---|
| 2.0 | 13.5% |
| 2.2 (free window) | 11.1% |
| 3.0 (standard) | 5.0% |
| 4.0 | 1.8% |

Two thresholds, deliberately different:

- **`kill_lambda = 3.0`** — the standard of evidence once money is
  committed (post free-window). ~5% false-kill.
- **`kill_lambda_free = 2.2`** — the looser standard *inside* the
  free-cancel window. A higher false-kill rate is the price of
  keeping the cancel free; waiting for λ=3 would push verdicts past
  the 10-minute refund deadline under scarcity.

The elegant property: **windows self-calibrate to difficulty.** At
~40 expected shares/h, λ=3 takes ~4.5 min; at ~10/h it takes ~18
min. The runner didn't get slower — it got exactly as patient as
the evidence requires. When the algo reprices shares again, the
gates follow automatically.

## Where it applies

All five share-verdict sites (all in `decide()`; each falls back to
the old fixed window when the value model can't produce
`predicted_h`):

| Gate | Was | Now |
|---|---|---|
| **Early free-window low_yield** (slot, age ≤ free_window) | `probe_judge_min`-ish fixed read | cancel only when below floor **and** λ ≥ `kill_lambda_free` since hashing start |
| **Never-produced dry** | `dry_after_min` (15m) | `max(dry_after_min, min(λ=3 window, max x_min))` — waits for evidence, but a rig expecting <3 shares/h isn't worth the doubt: the longest x arm (90m) still caps it |
| **Post-reroll arm** | `x_min` of silence after a re-roll | x arm **plus** λ ≥ `kill_lambda` since the re-roll — the new draw needs its own evidence window |
| **Incumbent low_yield** (produced but sub-floor → dry burst) | `x_min` window | `max(x_min, λ=3 window)` — reads the *widest* window so a sub-floor verdict still means something |
| **Probe maturity** (when the judge may rule) | `probe_judge_min` 5m / spot 20m | stretched to `kill_lambda_free` expected shares for *that probe's* it/s, bounded 1 min short of its cancel deadline |

## Bounds — patience is capped, not open-ended

- **On-demand probes can never outlive their free window:**
  evidence window is clamped to `probe_cancel_by_min − 1` (≈7 min).
  Under scarcity they don't reach full λ — they still judge earlier
  than a slot would, trading confidence for a free exit.
- **Spot probes** have no free window to lose (spot cancels are
  free anyway) — their window stretches to ~22 min, bounded by
  `spot_probe_judge_min`.
- **Truly dead draws don't ride forever:** the never-produced kill
  is capped at the longest x arm (90 min) regardless of λ — a rig
  expecting under ~3 shares/h is cancelled on patience grounds, not
  evidence.
- **Hardware faults bypass everything:** `zero_its`, `spec`/`gpu`
  mismatches, unreachable telemetry, and `same_tree` re-audition
  kills are not share-verdicts and fire immediately as before.

## The expectation source — and its caveat

`predicted_h` needs the value model's `S` (fleet-calibrated shares/h
at `its_ref`), `β` (sublinearity exponent), and `its_ref`. Until ~6
rig-hours of epoch telemetry accumulate, `S` runs on the
`default_shares_per_hour` prior — **lowered 300 → 50 when the λ
gates landed**, because epoch 240's real rate is ~15–37/h and a 6×
inflated expectation would keep every rig "below floor" no matter
how long the window. Watch the tick's `S=…` field: while it says
`(prior, provisional)` the gates are running on the 50/h guess;
once it reads `measured` they're self-calibrating.

If the value model is entirely absent (no `predicted_h`), every
site degrades to the pre-λ fixed windows — the change is strictly a
strictness improvement when data exists, invisible when it doesn't.

## Tuning guidance

- `kill_lambda` down (e.g. 2.5): kills sooner, accepts ~8%
  false-kills. Right if fees/pace matter more than marginal
  evidence — but remember a false kill costs a creation fee + a
  probe, so the break-even is further toward patience than it looks.
- `kill_lambda` up (4.0): ~2% false-kills, ~33% longer windows. If
  you keep seeing kills that later proved healthy, this is the knob.
- `kill_lambda_free` is bounded by physics: it must resolve inside
  `probe_cancel_by_min`/`free_window_min` or it's clamped anyway.
  Raising it toward 3 mainly affects spot probes and early slot
  judgements on fast rigs.
- **What healthy looks like post-change:** rigs living 15–20 min
  before verdict instead of dying at 5–8; `low_yield`/`dry` events
  thinning out; `S` flipping to measured. What unhealthy looks like:
  silent rigs riding to the 90-min x cap (means `predicted_h` is
  miscalibrated low — check `S`), or early kills still firing (means
  `S` is inflated high).

## What it doesn't fix

- A rig hashing at 10% of its rated it/s produces shares
  proportionally slow — λ accrues on *expected* shares, so a
  weak-but-alive rig keeps accumulating evidence slowly and reads as
  "low yield" once it produces. That's correct behaviour, but it
  means hardware-underperformance verdicts still rely on the it/s
  fault gates, not the share gates.
- The λ math assumes Poisson arrivals. Real share arrival is
  burstier (parent changes, tree luck), so the e^−λ numbers are the
  *ideal* false-kill rates — true rates are somewhat higher. The
  thresholds were picked with margin for that reason.

## Worked example

Epoch state: `S = 30` shares/h at `its_ref = 100`, `β = 1.0`,
`kill_lambda = 3.0`, `kill_lambda_free = 2.2`, `dry_after_min =
15`, longest x arm 90 min, free-cancel deadline ~10 min,
`probe_cancel_by_min` clamp ~7 min.

**Rig A — healthy 100 it/s slot that drew a quiet window.**
`predicted_h = 30 × (100/100)^1 = 30/h` → λ accrues at 0.5/min.

- t = 4.4 min: λ = 2.2 — a below-floor read may now cancel *free*
  (≈11% chance a healthy rig still shows nothing).
- t = 6 min: λ = 3 — full evidence; a healthy rig shows zero
  shares ~5% of the time.
- t = 15 min: `dry_after_min` floor reached. If still silent it's
  killed — the λ gate was satisfied 9 min ago, so here the fixed
  floor is what actually set the clock.

**Rig B — same rig, next epoch, shares repriced 4× scarcer**
(`S = 7.5/h`). `predicted_h = 7.5/h`.

- λ = 2.2 needs 17.6 min — past the free-cancel deadline, so no
  free verdict is possible; the rig ages out of the free window
  before the early gate can fire.
- Dry kill: window = `max(15, min(24, 90))` = 24 min. The old
  15-min fixed gate would have killed it at λ = 1.9 — a ~15%
  false-kill. The λ gate trades 9 extra minutes for ~5%.
- On-demand probe covering for it: evidence window clamped to
  ~7 min → λ ≈ 0.9, far short of 2.2. Under deep scarcity a probe
  judges on thin evidence — the documented trade for a free exit.

**Rig C — genuinely dead draw, `predicted_h = 2/h`.** λ = 3 would
need 90 min — exactly the longest x arm. Cancelled at 90 min on
patience grounds, not evidence: a rig expecting under ~3 shares/h
isn't worth more doubt.

Same code, three different clocks — the gates measured evidence,
not time.
