"""Replay decide() against a COPY of the live DB. Read-only:
build_state(apply_endings=False) never writes; decide() is pure.
Usage (from repo root):
    cp ~/.local/share/clorqub/clorqub.sqlite3{,-wal,-shm} /tmp/replay/ 2>/dev/null
    .venv/bin/python scripts/replay_decide.py /tmp/replay/clorqub.sqlite3 \
        > /tmp/replay/out.txt
"""
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import experiment  # noqa: E402

NOW = datetime.now(timezone.utc).replace(microsecond=0)


def main(db_path):
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    phase = db.execute(
        "SELECT * FROM experiment_phases WHERE closed_at IS NULL"
        " ORDER BY phase_id DESC LIMIT 1").fetchone()
    if phase is None:
        phase = db.execute(
            "SELECT * FROM experiment_phases ORDER BY phase_id DESC"
            " LIMIT 1").fetchone()
    if phase is None:
        print("no experiment phase in db")
        return
    cfg = experiment.validate_config(json.loads(phase["config_json"]))
    state = experiment.build_state(
        db, phase, NOW, cfg, apply_endings=False, say=lambda *_a: None)

    notes = []
    acts = experiment.decide(NOW, state, cfg, say=notes.append)

    print(f"now={NOW.isoformat()} phase={phase['phase_id']}")
    print(f"budget={state['budget']} committed={state['committed']:.2f}"
          f" remaining={state['remaining']:.2f}")
    p = experiment.pace(state, NOW, cfg)
    print("pace:", {k: round(v, 4) if isinstance(v, float) else v
                     for k, v in p.items()})
    print(f"rentals: {len(state['rentals'])}")
    for r in state["rentals"]:
        print(f"  order={r['order_id']} sid={r['server_id']} "
              f"role={r['role']} its={r['its_total']} "
              f"its_last={r['its_last']} hourly={r['hourly_usd']} "
              f"age_min={int(r['age'].total_seconds() // 60)} "
              f"shares={sum(c for _, c in r['share_arrivals'])}")
    print("\n--- notes ---")
    for n in notes:
        print(n)
    print("\n--- actions ---")
    for a in acts:
        print(json.dumps(a, default=str))
    active_ids = {r["server_id"] for r in state["rentals"]}
    term = max(1, min(cfg["hours"], int(p["hours_to_epoch"])))
    cands = experiment._candidates(state, cfg, active_ids, [],
                                   term=term)
    print(f"\n--- top 10 candidates (term={term}h) ---")
    print(f"{'sid':>7} {'gpu':<10} {'cpu':<28} {'hourly':>7} "
          f"{'est_its':>8} {'prior':>8} extrap")
    for c in cands[:10]:
        prior = (f"{c['prior']:>8.1f}" if c["prior"] is not None
                 else f"{'-':>8}")
        print(f"{c['server_id']:>7} {str(c['gpu']):<10} "
              f"{str(c['cpu'])[:28]:<28} "
              f"{float(c['hourly']):>7.3f} "
              f"{str(c['est_its']):>8} {prior} {c['extrapolated']}")
    db.close()


if __name__ == "__main__":
    main(sys.argv[1])
