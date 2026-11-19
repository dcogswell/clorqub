# Experiment runner — decision flow

Maps `experiment.py` (`decide` → `execute`, one pass per tick). Keep
this next to the code: when the logic changes, update the matching
section — a stale diagram is worse than none.

## Per-tick pipeline

`decide()` emits action dicts in this order; `execute()` applies
cancels first, then bursts, the judgement, then rents.

```mermaid
flowchart TD
    ST[build_state:<br>rentals, telemetry, quotes,<br>pacing, reservations] --> BL{blocks?}
    BL --> B1["budget — remaining spent"]
    BL --> B2["term — &lt;1h to phase/epoch end"]
    BL --> B3["probe_guard — non-spot probe<br>near free-window deadline"]
    BL --> B4["lull — yield-failure streak pause"]

    subgraph L["1 · per-rental checks — cancels run even when blocked"]
        direction TB
        L1["probe past cancel deadline<br>→ cancel probe_deadline"]
        L2["errors-only telemetry<br>→ cancel unreachable"]
        L3["re-audition drew same tree<br>→ cancel same_tree"]
        L4["inside free window + fault<br>→ cancel fault:spec|zero_its|gpu_*"]
        L5["slot: low_yield / dry<br>after λ ≥ kill_lambda evidence<br>→ cancel + schedule retry_burst"]
        L6["slot: produced but below floor<br>→ emit 'dry' burst — incumbent rides it out"]
    end
    BL --> L

    subgraph BM["2 · burst emission — concurrent bursts,<br>cap = max_rentals unjudged; phase open, no blocks"]
        direction TB
        M1["manual — operator-requested n probes"]
        M2["succession — slot expiring, no auto_renew"]
        M3["upgrade — challenger predicted to beat weakest slot;<br>also runs when vacancy exists but pace/budget blocks fills"]
        M4["dry_retry — a scheduled retry burst came due<br>(retry_expired if past grace)"]
    end
    L --> BM

    subgraph JD["3 · judge — one verdict per cycle,<br>oldest mature burst first"]
        J1{"all probes matured (λ-stretched<br>window) or stall deadline?"} -->|yes| J2[verdict → cancels / promote /<br>renew / re-arm]
    end
    BM --> JD

    subgraph FL["4 · fills — skipped when blocked or lull"]
        direction TB
        F1["probe fills — every live burst tops up to n_target,<br>shared cap = max_rentals + burst_max, same cycle"]
        F2["due pending_rerents — re-rent the same server"]
        F3["vacancy fills — best combo under headroom + budget;<br>pending_rerents reserve slots; retries do not"]
        F4["never-idle fallback — all slots empty, nothing reserved"]
        F5["loss_mining — floor/return gates dropped:<br>ranking is pure score; picks flagged"]
    end
    JD --> FL
    FL --> EX["execute(): one slot rent per cycle,<br>probe rents unlimited"]
```

## Order lifecycle

```mermaid
stateDiagram-v2
    [*] --> slot: rent reason=fill|rerent
    [*] --> probe: rent reason=probe (burst fill)

    probe --> promoted: finder wins at judgement
    promoted --> slot: role flipped probe→slot
    probe --> cancelled: probe_lost (found shares, lost)<br>probe_no_share / fault:* /<br>unreachable / same_tree /<br>probe_deadline

    slot --> cancelled: fault:* / unreachable /<br>low_yield / dry / dry_after_x /<br>low_yield_after_x / term_end /<br>replaced|upgraded (judge) /<br>dry (judge, nothing produced)

    cancelled --> pending_rerent: rerent_at set —<br>same server, re-rented when due
    pending_rerent --> slot: rerent fills pass same gates
    cancelled --> pending_retry: retry_burst —<br>fresh auditions after y_min arm
    pending_retry --> burst: burst_scheduled

    cancelled --> blacklisted: fault:* / unreachable —<br>3d TTL, excluded from pool
    cancelled --> [*]: ended
    blacklisted --> [*]
```

## Burst lifecycle

```mermaid
stateDiagram-v2
    [*] --> pending: burst_scheduled<br>(dry retry waits out its y_min arm)
    pending --> active: due → burst_started,<br>probes fill same cycle
    pending --> [*]: retry_expired — past grace

    [*] --> active: dry / manual / succession /<br>upgrade / reactivated

    active --> judged: probes matured or stall deadline →<br>burst_judged

    judged --> upgraded: challenger beat incumbent<br>by upgrade_margin (succession/upgrade)
    judged --> promoted: finder takes a vacancy —<br>original had none or lost outright
    judged --> displaced: full fleet — finder beat weakest<br>incumbent by upgrade_margin
    judged --> kept_original: incumbent holds<br>(auto_renew or margin not cleared)
    judged --> renewed: succession + no auto_renew →<br>rerent scheduled at deadline
    judged --> rearmed: dry_retry / retry_again —<br>nothing produced and a vacancy is open,<br>re-arm after y_min
    judged --> closed: no_vacancy (finders, fleet full,<br>margin not cleared) / no_finders
```

## Verdicts & log vocabulary

| verdict | meaning |
|---|---|
| `upgraded` | challenger beat incumbent's forecast by `upgrade_margin`; incumbent cancelled `upgraded`, loser probes dropped |
| `promoted` | a finder took the slot — vacancy fill or weak-incumbent replace |
| `displaced` | fleet full: finder beat the weakest incumbent by `upgrade_margin`; incumbent cancelled `displaced` |
| `kept_original` | incumbent survives; all probes cancelled |
| `renewed` | succession: incumbent had no auto_renew → rerent scheduled at its deadline |
| `dry_retry` | burst + incumbent both produced nothing; incumbent cancelled `dry`, retry re-armed |
| `retry_again` | dry_retry burst, nothing produced, slot still open → re-arm |
| `no_vacancy` | finders existed but the fleet was full and none cleared `upgrade_margin` vs the weakest incumbent; probes dropped |
| `no_finders` | no probe produced a share and nothing else applied |

| trigger | why the burst exists |
|---|---|
| `dry` | slot produced but below yield floor — probes audition against the riding incumbent |
| `succession` | slot term ending, no auto_renew — audition a replacement before it expires |
| `upgrade` | a listing looks better than the weakest slot (fleet full, or fills blocked) |
| `dry_retry` | scheduled re-audition after a dry/low-yield cancel, after its y_min arm — reserves nothing |
| `reactivated` | resumed spot order gets an audition burst |
| `manual` | operator-requested burst |

| other terms | meaning |
|---|---|
| `finder` | probe that landed ≥1 share during its audition window |
| `econ_finder` | finder whose realized return also clears `min_return` |
| `tree_retry_order_ids` | healthy no-share probes tagged for a bounded re-audition (upgrade judgements only) |
| `gross_tiebreak` | under `score=gross`: gross $/h within this fraction is a near-tie — the contest is then arbitrated on net $/h instead, and the cheaper earner of the same revenue wins; judge stats report `tiebreak: "net"` when arbitration applied |
| `kill_lambda` / `kill_lambda_free` | dry/low-yield verdicts wait for λ expected shares (λ = predicted share-rate × hours), not a fixed clock — windows stretch with pool share difficulty; `_free` is the looser standard inside the free-cancel window. See [poisson-judging.md](poisson-judging.md) |
| `probe_guard` | block: an on-demand probe is near its free-cancel deadline; new rents wait |
| `pending_bursts` / `pending_rerents` | scheduled retries / re-rents; only `pending_rerents` reserve a vacancy in the fill math |
| `pending` (block) | a rent attempt parked `unknown`/`creating` — an ambiguous POST could mean a live order, so all actions freeze until it resolves |
| `attempt_recheck` | parked attempts older than `attempt_recheck_min` are re-polled against `my_orders` at that interval; `attempt_recheck_clear` consecutive clean fetches auto-resolve to `no_order`, a candidate keeps it parked for manual `rent.py reconcile` |

## Retries reserve nothing

A `dry_retry` is a pure audition: it fires when due whether or not a
vacancy exists, and the fill math ignores it. If a cold fill took the
slot meanwhile, a finder that still wants in must displace the weakest
incumbent by `upgrade_margin` (verdict `displaced`); otherwise the
judgement is `no_vacancy` and the chain ends — the upgrade scan owns
churn on a full fleet.
