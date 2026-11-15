#!/usr/bin/env python3
"""On-demand read-only miner log tail over the same guarded SSH path.

One explicit `tail` per invocation -- no polling, no writes, no API.
"""

import rig_ssh

POOLS = ("jetski", "qli")
MAX_CHARS = 200_000


def tail_miner_log(host, port, key_path, order_id, pool, lines=200):
    """Return the last `lines` of /clorqub/logs/<pool>.log on the rig.

    pool must be jetski or qli (the only log files the agent writes);
    lines is an int in 1..2000. Output capped at MAX_CHARS (tail kept).
    """
    if pool not in POOLS:
        raise ValueError("pool must be jetski or qli")
    if isinstance(lines, bool) or not isinstance(lines, int) \
            or not 1 <= lines <= 2000:
        raise ValueError("lines must be an integer 1..2000")
    out = rig_ssh.ssh_text(
        host, port, key_path, order_id,
        f"tail -n {lines} /clorqub/logs/{pool}.log")
    if len(out) > MAX_CHARS:
        out = out[-MAX_CHARS:]
    return out


def fetch_log_chunk(host, port, key_path, order_id, pool, offset,
                    max_bytes=200_000):
    """Return {"chunk", "remote_size"}: the bytes of <pool>.log past `offset`.

    Prints remote file size first (CQSIZE:<n>) so the caller can track a
    cursor; on shrink/rotation (remote_size < offset) returns the last
    64 KiB instead. Caller rule: next cursor is remote_size when
    remote_size <= offset, else offset + len(chunk bytes) -- and on
    shrink the chunk starts at max(0, remote_size - len(chunk bytes)).
    """
    if pool not in POOLS:
        raise ValueError("pool must be jetski or qli")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("offset must be an integer >= 0")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) \
            or not 1 <= max_bytes <= 1_000_000:
        raise ValueError("max_bytes must be an integer 1..1000000")
    out = rig_ssh.ssh_text(
        host, port, key_path, order_id,
        f'f=/clorqub/logs/{pool}.log; '
        f's=$(wc -c < "$f" 2>/dev/null || echo 0); '
        f'printf "CQSIZE:%s\\n" "$s"; '
        f'if [ "$s" -gt {offset} ]; then '
        f'tail -c +{offset + 1} "$f" | head -c {max_bytes}; '
        f'elif [ "$s" -lt {offset} ]; then tail -c 65536 "$f"; fi')
    first, sep, chunk = out.partition("\n")
    if not sep or not first.startswith("CQSIZE:"):
        raise ValueError("unexpected log fetch reply")
    try:
        remote_size = int(first[len("CQSIZE:"):])
    except ValueError:
        raise ValueError("unexpected log size reply") from None
    if remote_size < 0:
        raise ValueError("unexpected log size reply")
    return {"chunk": chunk, "remote_size": remote_size}
