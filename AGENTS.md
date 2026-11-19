# Clorqub — working conventions

## Git

- **Commit after every change** (user preference, standing since
  2026-11-19). Commit as you go; do not leave a dirty tree unless the
  user says otherwise.
- No git identity is configured locally. Pass it per commit:
  `git -c user.name="Devin" -c user.email="158243242+devin-ai-integration[bot]@users.noreply.github.com" commit ...`
- Push when the change affects rig bootstrapping (rig/ files must be
  on origin for the pinned commit to resolve) or when asked.

## Bootstrap repin

Rigs fetch `rig/{bootstrap.sh,agent.py,qubic_parse.py,SHA256SUMS}`
from `raw.githubusercontent.com/dcogswell/clorqub/<BOOTSTRAP_COMMIT>`.
To ship an agent change:

1. Edit `rig/*`, regenerate `rig/SHA256SUMS`
   (`cd rig && sha256sum agent.py qubic_parse.py > SHA256SUMS`), commit
   + push.
2. Update `rental_policy.BOOTSTRAP_COMMIT` to the new sha
   (`BOOTSTRAP_SHA256` only changes if `bootstrap.sh` itself changed).
   Commit that as the repin commit.
3. Restart the runner so `_exec_rent` picks up the new pin.

## Processes

- Runner: `experiment.py run --live --confirm-budget <phase budget>`
  — restart it after changing `experiment.py`/`rental_policy.py`/
  `telemetry_policy.py` (imports are in-memory). Kill by PID, then
  `bash scripts/runner_watchdog.sh` (do NOT `pgrep -f` inside a
  compound command — it self-matches the wrapper).
- `scripts/runner_watchdog.sh` is cron-intended but cron was observed
  not firing — run it manually after kills.
- Telemetry collector (`cli.py scan --watch`) imports
  `telemetry_policy` at start; restart it after whitelist changes.

## Tests

`.venv/bin/python -m unittest tests.test_blacklist tests.test_cli
tests.test_experiment tests.test_market tests.test_pool_stats
tests.test_rig tests.test_spot tests.test_tui tests.test_tui_data`

## Known pitfalls

- jetski renders rates > ~1k as "1.17k it/s" — parse via
  `qubic_parse.rate_value`, never plain `int()`.
- `rig/agent.py` clamps `CPU_THREADS <= 0` to `nproc-1` — zero is not
  a disable. The cap lives in rental quoting, not agent semantics.
- `cpu_blocklist_re` (default `\bEPYC\s+[1-8]`) gates *automatic*
  candidates only; manual rentals bypass it.
- Score weighting adjusts yield/ranking only; dry/no-share conviction
  stays share-based (Poisson expected-share thresholds).
