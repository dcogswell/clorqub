"""Command specification for an opt-in, transient systemd user collector."""

import sys
from pathlib import Path

UNIT = "cq-scanner.service"
DESCRIPTION = "clorqub read-only marketplace scanner"


def start_command(db_path, env_file, interval, spot_limit=0,
                  tick_interval=None):
    """Use the existing bounded, non-overlapping scanner; never pass secrets."""
    repo = Path(__file__).resolve().parent
    argv = [
        "systemd-run", "--user", f"--unit={UNIT}",
        f"--description={DESCRIPTION}", "--service-type=exec", "--collect",
        "--property=UMask=0077", "--property=Restart=no",
        f"--property=WorkingDirectory={repo}",
        "--", sys.executable, "-u", str(repo / "cli.py"),
        "scan", "--live", "--watch", "--interval", str(interval),
        "--db", str(Path(db_path).expanduser().resolve()),
        "--env-file", str(Path(env_file).expanduser().resolve()),
        "--spot-limit", str(spot_limit),
    ]
    if tick_interval is not None:
        argv += ["--tick-interval", str(tick_interval)]
    return argv
