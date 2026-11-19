#!/usr/bin/env python3
"""Periodic fleet report -> markdown file in the reports dir.

Renders build_state + local DB evidence into a snapshot the operator
can diff half-hour to half-hour: fleet production, judging activity,
pool-share position vs target, money, and watch items.

Usage: fleet_report.py [--db PATH] [--out DIR]
Cron:  */30 * * * * /home/daryl/clorqub/scripts/fleet_report.py
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import experiment  # noqa: E402

DB = os.path.expanduser("~/.local/share/clorqub/clorqub.sqlite3")
OUT = os.path.join(REPO, "reports")  # gitignored; visible in the IDE

# event kinds worth reporting; tick/rental_value/trainer are noise
REPORT_KINDS = {
    "burst_judged", "rent", "cancel", "first_share", "ended",
    "rent_failed", "cancel_failed", "blacklisted", "renewed",
    "tree_wait", "lull", "rerent_scheduled", "rerent_expired",
    "unpinned", "data_cutoff", "attempt_recheck", "blocked",
    "burst_started", "burst_scheduled", "note",
}


def _iso(value):
    return value.isoformat() if isinstance(value, datetime) else value


def _dt(value):
    return value if isinstance(value, datetime) \
        else datetime.fromisoformat(value)


def _usd(v, p=2):
    return "—" if v is None else f"${float(v):.{p}f}"


def _n(v, p=0):
    return "—" if v is None else f"{float(v):,.{p}f}"


def _ago(now, ts):
    if ts is None:
        return "?"
    return f"{max(0, int((now - _dt(ts)).total_seconds() // 60))}m"


def _fmt_event(ev, order_server):
    """One report line for an experiment event."""
    kind, det = ev["kind"], ev["detail"]
    ts = ev["ts"][11:16]
    oid = ev["order_id"]
    if kind == "burst_judged":
        v = det.get("verdict") or "?"
        st = det.get("stats") or {}
        if v == "earned_seat":
            return (f"{ts} burst {det.get('burst_id')} -> earned_seat: "
                    f"probe {det.get('promote')} "
                    f"({_n(st.get('probe_its'))} its) net "
                    f"{_usd(st.get('probe_net_usd_hr'))}/h > seat bar "
                    f"{_usd(st.get('seat_bar'))} -> promoted")
        if st.get("probe_net_usd_hr") is not None:
            return (f"{ts} burst {det.get('burst_id')} -> {v}: "
                    f"net {_usd(st.get('probe_net_usd_hr'))}/h vs bar "
                    f"{_usd(st.get('seat_bar'))} -> rejected")
        cancelled = det.get("cancelled") or []
        extra = f" ({len(cancelled)} cancelled)" if cancelled else ""
        return f"{ts} burst {det.get('burst_id')} -> {v}{extra}"
    if kind == "note":
        return f"{ts} {det.get('text', '')[:160]}"
    if kind == "rent":
        srv = order_server.get(oid, f"order {oid}")
        return (f"{ts} rent {oid} {srv} {det.get('reason', '')} "
                f"{det.get('hours')}h @ {_usd(det.get('hourly_usd'))}/h"
                f" ({det.get('mode')})")
    if kind == "cancel":
        srv = order_server.get(oid, f"order {oid}")
        why = det.get("reason") or det.get("verdict") or ""
        return f"{ts} cancel {oid} {srv} {why}".rstrip()
    if kind == "rent_failed":
        return f"{ts} rent_failed {det.get('kind', '')}: " \
               f"{str(det.get('error') or det.get('detail') or '')[:100]}"
    if kind == "first_share":
        return f"{ts} first_share {oid} " \
               f"{order_server.get(oid, '')}".rstrip()
    if kind == "ended":
        srv = order_server.get(oid, f"order {oid}")
        return f"{ts} ended {oid} {srv} {det.get('reason', '')}".rstrip()
    if kind == "blacklisted":
        return f"{ts} blacklisted {det.get('server_id')} " \
               f"{det.get('reason', '')}".rstrip()
    if kind == "tree_wait":
        return f"{ts} tree_wait {oid} tree {det.get('tree_id')}: " \
               f"{str(det.get('note') or '')[:100]}"
    if kind == "lull":
        return f"{ts} lull streak={det.get('streak')}: " \
               f"{str(det.get('note') or '')[:100]}"
    if kind in ("burst_started", "burst_scheduled"):
        return f"{ts} {kind} {det.get('burst_id', '')} " \
               f"-> {det.get('n', det.get('n_target', '?'))} probes"
    return f"{ts} {kind} {oid or ''} " \
           f"{json.dumps(det, default=str)[:110]}"


def report(db_path, now):
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    phase = db.execute(
        "SELECT * FROM experiment_phases WHERE closed_at IS NULL"
        " ORDER BY phase_id DESC LIMIT 1").fetchone()
    L = []
    if phase is None:
        L.append("# Fleet report — " + _iso(now)[:16].replace("T", " ")
                 + " UTC\n\nno open experiment phase.\n")
        return "\n".join(L)
    cfg = experiment.validate_config(json.loads(phase["config_json"]))
    state = experiment.build_state(
        db, dict(phase), now, cfg, apply_endings=False,
        say=lambda *a: None)
    pacing = experiment.pace(state, now, cfg)
    ps = state.get("pool_share") or {}
    snap = db.execute(
        "SELECT * FROM pool_snapshots ORDER BY source_ts DESC"
        " LIMIT 1").fetchone()

    L.append(f"# Fleet report — {_iso(now)[:16].replace('T', ' ')} UTC")
    head = [f"epoch {snap['epoch'] if snap else '?'}"]
    if ps.get("epoch_h_left") is not None:
        head.append(f"~{ps['epoch_h_left']:.0f}h epoch left")
    head.append(f"phase {phase['phase_id']}")
    head.append(f"runner {'alive' if experiment.runner_alive(db_path) else 'DEAD'}")
    L.append(" · ".join(head) + "\n")

    # --- fleet ---
    live = state["rentals"]  # build_state only returns active rows
    L.append(f"## Fleet — {len(live)} active "
             f"(slot target {state.get('slot_target')})\n")
    if live:
        vm = state.get("value_model") or {}
        price, w_bar = vm.get("P"), vm.get("w_bar")
        L.append("| order | server | role | its | shr/h | sps | "
                 "$/hr | realized $/h | age | obs |")
        L.append("|---|---|---|---|---|---|---|---|---|---|")
        for r in sorted(live, key=lambda r: (r["role"] != "slot",
                                             -float(r["its_total"]
                                                    or 0))):
            hours_r = None
            if r["t0"] is not None:
                hours_r = max(0.0, (now - r["t0"]
                                    - timedelta(minutes=2)
                                    ).total_seconds() / 3600)
            shr_h = (sum(c for ts, c in r["share_arrivals"]
                         if ts >= r["t0"] + timedelta(minutes=2))
                     / hours_r if hours_r else None)
            realized = (shr_h * w_bar * price
                        if (r["role"] == "slot" and shr_h is not None
                            and price and w_bar) else None)
            L.append(
                f"| {r['order_id']} | s{r['server_id']} | {r['role']} | "
                f"{_n(r['its_total'])} | {_n(shr_h, 1)} | "
                f"{_n(r.get('score_per_share'))} | "
                f"{_usd(r['hourly_usd'], 3)} | {_usd(realized, 3)} | "
                f"{int(r['age'].total_seconds() // 60)}m | "
                f"{_ago(now, r.get('last_good_at'))} |")
    else:
        L.append("no active rentals.")
    L.append("")

    # --- judging ---
    since = (now - timedelta(minutes=35)).isoformat()
    rows = db.execute(
        "SELECT ts, order_id, server_id, kind, detail_json"
        " FROM experiment_events WHERE ts >= ? ORDER BY ts",
        (since,)).fetchall()
    order_server = {r["order_id"]: f"s{r['server_id']}"
                    for r in state["rentals"]}
    seen_note, lines = set(), []
    for r in rows:
        det = json.loads(r["detail_json"] or "{}")
        ev = {"ts": r["ts"], "kind": r["kind"], "order_id":
              r["order_id"], "detail": det}
        if ev["kind"] not in REPORT_KINDS:
            continue
        line = _fmt_event(ev, order_server)
        if ev["kind"] == "note":
            if line in seen_note:
                continue
            seen_note.add(line)
        lines.append("- " + line)
    L.append("## Judging — last 35m\n")
    L.extend(lines if lines else ["- quiet"])
    L.append("")

    # --- pool share ---
    L.append("## Pool share\n")
    if ps:
        share = f"{ps['share_now_pct']:.2f}%" \
            if ps.get("share_now_pct") is not None else "—"
        L.append(f"share now **{share}** "
                 f"({_n(ps.get('our_sol'))} sol / "
                 f"{_n(ps.get('total_sol'))}) · "
                 f"sol/its {_n(ps.get('sol_per_its'), 1)} measured · "
                 f"pool {_n(ps.get('pool_miners'))} miners")
        if ps.get("enabled"):
            cost_epoch = (f"{_usd(ps['est_cost_epoch'])} over "
                          f"{_n(ps['epoch_h_left'])}h left"
                          if ps.get("est_cost_epoch") is not None
                          else "—")
            L.append(
                f"**target {ps['target_pct']}%** -> needs "
                f"{_n(ps.get('needed_sol'))} sol ≈ "
                f"{_n(ps.get('needed_its'))} it/s -> est "
                f"{_usd(ps.get('est_cost_hr'))}/hr · "
                f"{_usd(ps.get('est_cost_day'))}/day · {cost_epoch}")
        else:
            L.append(f"targeting **off** (target "
                     f"{ps.get('target_pct')}% configured — "
                     "enable in Settings › Strategy)")
        L.append(f"_eff basis {_n(ps.get('eff_its_per_usd'))} its "
                 f"per $/hr over {ps.get('eff_n', 0)} proven stints · "
                 f"pool snap {_ago(now, ps.get('snapshot_ts'))} old_")
    else:
        L.append("no pool snapshot yet.")
    L.append("")

    # --- money ---
    L.append("## Money\n")
    L.append(
        f"committed **${state['committed']:.2f}** / "
        f"${float(phase['budget_usd']):.2f} · remaining "
        f"**${state['remaining']:.2f}** · burn "
        f"${state['active_burn_hr']:.3f}/h vs optimal "
        f"${pacing['sustainable_hr']:.3f}/h "
        f"(headroom ${pacing['headroom_hr']:.3f}/h)")
    if snap is not None:
        L.append(
            f"wallet counted {_n(snap['counted'])} · "
            f"v={_usd(snap['usd_per_counted'], 4)}/counted · "
            f"est_gains {_usd(snap['est_gains_usd'])} "
            f"(epoch {snap['epoch']})")
    L.append("")

    # --- watch ---
    L.append("## Watch\n")
    watch = []
    live_aliases = {a for (a,) in db.execute(
        "SELECT fr.alias FROM fleet_rentals fr"
        " JOIN experiment_rentals er ON er.order_id=fr.order_id"
        " WHERE er.ended_at IS NULL AND fr.alias IS NOT NULL")}
    if snap is not None:
        for w in db.execute(
                "SELECT w.alias, w.sol FROM pool_worker_snapshots w"
                " JOIN pool_snapshots s ON s.id=w.snapshot_id"
                " WHERE s.source_ts=? ORDER BY w.sol DESC",
                (snap["source_ts"],)):
            if w["alias"] in live_aliases or not w["sol"]:
                continue
            was_rental = db.execute(
                "SELECT er.order_id, er.ended_at FROM fleet_rentals fr"
                " JOIN experiment_rentals er"
                "   ON er.order_id=fr.order_id WHERE fr.alias=?",
                (w["alias"],)).fetchone()
            if was_rental:
                watch.append(
                    f"- tail: `{w['alias']}` sol={w['sol']:.0f} still "
                    f"credited ~{_ago(now, was_rental['ended_at'])} "
                    f"after rental end (order "
                    f"{was_rental['order_id']})")
    for r in live:
        age = r.get("obs_age")
        if age is not None and age > timedelta(minutes=10):
            watch.append(
                f"- stale obs: order {r['order_id']} s{r['server_id']} "
                f"last good obs {int(age.total_seconds() // 60)}m ago")
    pend = state.get("pending_rerents") or []
    for p in pend[:4]:
        watch.append(f"- rerent s{p['server_id']} due "
                     f"{_iso(p['due_at'])[11:16]}")
    for b in (state.get("unjudged_bursts") or [])[:3]:
        watch.append(f"- burst {b['burst_id']} unjudged "
                     f"({b.get('n_target', '?')} probes, "
                     f"started {_ago(now, b.get('started_at'))} ago)")
    watch.append(f"- dead trees known: {len(state['dead_trees'])}")
    score_ref = (state.get("stats") or {}).get("score_ref")
    if score_ref:
        watch.append(f"- score_ref {_n(score_ref)} "
                     f"(fleet median sps baseline)")
    L.extend(watch if watch else ["- nothing flagged"])
    L.append("")
    db.close()
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB)
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    os.makedirs(args.out, exist_ok=True)
    text = report(os.path.expanduser(args.db), now)
    path = os.path.join(
        args.out, now.strftime("%Y-%m-%d_%H%M") + ".md")
    with open(path, "w") as fh:
        fh.write(text)
    print(path)


if __name__ == "__main__":
    main()
