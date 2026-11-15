"""Minimal, safe SSH transport to a rented rig (stdlib, argv lists only).

Never puts credentials on the command line, in the environment, or in
logs; remote stderr is discarded so server-side output can't leak back
into ours. Host/port are re-validated here so callers (including
enroll) can't feed shell metacharacters into the argv.
"""

import json
import os
import re
import subprocess

HOST_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}")

# atomic: tmp write -> chmod -> rename, every step chained so a cat
# failure can't leave a partial/masked result
CREDS_CMD = (
    "umask 077; mkdir -p /clorqub && "
    "cat > /clorqub/credentials.json.tmp && "
    "chmod 600 /clorqub/credentials.json.tmp && "
    "mv /clorqub/credentials.json.tmp /clorqub/credentials.json")
STATUS_CMD = "cat /clorqub/status.json"


class SSHError(Exception):
    pass


def _check_endpoint(host, port):
    if not isinstance(host, str) or not HOST_RE.fullmatch(host) \
            or ".." in host:
        raise SSHError("invalid host")
    if isinstance(port, bool) or not isinstance(port, int) \
            or not 1 <= port <= 65535:
        raise SSHError("invalid port")


def _known_hosts(order_id):
    config = os.path.expanduser("~/.config/clorqub")
    os.makedirs(config, mode=0o700, exist_ok=True)
    try:
        os.chmod(config, 0o700)
    except OSError:
        pass
    return os.path.join(config, f"known_hosts-{order_id}")


def _argv(host, port, key_path, order_id, command):
    return [
        "ssh", "-T",
        "-i", os.path.expanduser(key_path),
        "-o", "BatchMode=yes",
        "-o", "IdentitiesOnly=yes",
        "-o", "ForwardAgent=no",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", f"UserKnownHostsFile={_known_hosts(order_id)}",
        "-o", "ConnectTimeout=10",
        "-p", str(int(port)),
        f"root@{host}",
        command,
    ]


def _run(argv, stdin=None):
    try:
        cp = subprocess.run(argv, input=stdin, capture_output=True,
                            text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        raise SSHError("ssh transport failed") from None
    if cp.returncode != 0:
        # deliberately no remote stderr: it may contain anything
        raise SSHError(f"ssh exited {cp.returncode}")
    return cp.stdout


def push_credentials(host, port, key_path, order_id, credentials):
    """Upload /clorqub/credentials.json via stdin only (mode 600)."""
    _check_endpoint(host, port)
    _run(_argv(host, port, key_path, order_id, CREDS_CMD),
         stdin=json.dumps(credentials))


def ssh_text(host, port, key_path, order_id, command):
    """Run a read-only remote command; returns stdout str. Same
    endpoint validation and stderr-dropping guarantees as ssh_json."""
    _check_endpoint(host, port)
    return _run(_argv(host, port, key_path, order_id, command))


def restart_miner(host, port, key_path, order_id, pool):
    """Kill the miner process; the rig agent restarts it ~5 s later
    (that is the deliberate re-roll -- no agent change needed).
    Returns stripped stdout ('restarted' or 'none')."""
    if pool not in ("jetski", "qli"):
        raise SSHError("invalid pool")
    _check_endpoint(host, port)
    command = (f"pkill -TERM -f '^/clorqub/pkgs/{pool}/' "
               "&& echo restarted || echo none")
    return _run(_argv(host, port, key_path, order_id, command)).strip()


def ssh_json(host, port, key_path, order_id):
    """Read /clorqub/status.json; returns the parsed dict."""
    _check_endpoint(host, port)
    out = _run(_argv(host, port, key_path, order_id, STATUS_CMD))
    try:
        return json.loads(out)
    except ValueError:
        raise SSHError("invalid status json") from None
