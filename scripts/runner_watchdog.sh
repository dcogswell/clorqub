#!/usr/bin/env bash
# clorqub experiment-runner watchdog.
#
# Guards both failure modes seen in the wild:
#   - dead process while a phase is still open     -> restart
#   - alive-but-wedged (heartbeat stale > STALE_S) -> kill -9, restart
#
# Run from cron every minute. Exits 0 quietly when no phase is open
# (a respawned runner would only print "no open experiment phase").
# --confirm-budget is read from the open phase row, never hardcoded,
# so it can't silently confirm a different budget.

set -u

REPO="/home/daryl/clorqub"
DB="$HOME/.local/share/clorqub/clorqub.sqlite3"
RUNLOG="/tmp/clorqub-runner.log"
WDLOG="/tmp/clorqub-watchdog.log"
STALE_S=600            # >2x the throttled ~5min tick-event cadence;
                       # tighter risks killing a healthy runner

log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" >>"$WDLOG"; }

row=$(sqlite3 -separator '|' "$DB" \
    "SELECT phase_id, budget_usd FROM experiment_phases
     WHERE closed_at IS NULL ORDER BY phase_id DESC LIMIT 1" 2>/dev/null)
[ -z "$row" ] && exit 0
phase_id="${row%%|*}"
budget="${row##*|}"

start_runner() {
    nohup "$REPO/.venv/bin/python" -u "$REPO/experiment.py" run \
        --live --confirm-budget "$budget" --db "$DB" \
        >>"$RUNLOG" 2>&1 &
    log "started runner (phase $phase_id, budget \$$budget, pid $!)"
}

# runner PIDs: cmdline match + comm=python — a bash -c wrapper whose
# cmdline carries the launch string must not be killable here
pids=$(for p in $(pgrep -f "experiment[.]py run --live" || true); do
    [ "$(cat /proc/$p/comm 2>/dev/null)" = "python" ] && echo "$p"
done)

if [ -z "$pids" ]; then
    log "phase $phase_id open but no runner process; restarting"
    start_runner
    exit 0
fi

# heartbeat = latest 'tick' event the runner emits each cycle
age_s=$(sqlite3 "$DB" \
    "SELECT COALESCE(
        (julianday('now') - julianday(MAX(ts))) * 86400, -1)
     FROM experiment_events WHERE kind='tick'" 2>/dev/null)
age_s=${age_s%%.*}
: "${age_s:=-1}"

if [ "$age_s" -gt "$STALE_S" ]; then
    # wedge only if the runner itself is old enough to have ticked
    oldest=$(ps -o etimes= -p $pids 2>/dev/null | sort -rn | head -1)
    if [ "${oldest:-0}" -gt "$STALE_S" ]; then
        log "runner wedged (tick heartbeat ${age_s}s stale); killing $pids"
        kill -9 $pids 2>/dev/null
        sleep 2
        start_runner
    fi
fi
exit 0
