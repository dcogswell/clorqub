"""Opt-in scheduling for the read-only scanner; no background auto-start."""

import math
import time

DEFAULT_INTERVAL = 300.0
MIN_INTERVAL = 15.0


def run_watch(scan_once, interval, stop_event, tick=None,
              tick_interval=30.0, extra_tick=None,
              extra_tick_interval=None):
    """Retry transient fetch failures; configuration/storage errors stop the loop."""
    if tick is not None or extra_tick is not None:
        return _run_with_tick(
            scan_once, interval, stop_event, tick, tick_interval,
            extra_tick=extra_tick,
            extra_tick_interval=extra_tick_interval)
    while not stop_event.is_set():
        status = scan_once()
        if status == 2:
            return 2
        # Wait after completion, so slow requests never cause overlapping scans
        # or a burst of catch-up calls. Event.wait permits prompt shutdown.
        if stop_event.wait(interval):
            break
    return 0


def _run_with_tick(scan_once, interval, stop_event, tick, tick_interval,
                   extra_tick=None, extra_tick_interval=None):
    """Independent local SSH cadence; never accelerates marketplace API polling."""
    next_scan = 0.0
    next_tick = 0.0 if tick is not None else math.inf
    next_extra = 0.0 if extra_tick is not None else math.inf
    while not stop_event.is_set():
        if time.monotonic() >= next_scan:
            if scan_once() == 2:
                return 2
            next_scan = time.monotonic() + interval
        if stop_event.is_set():
            break
        if tick is not None and time.monotonic() >= next_tick:
            if tick() == 2:
                return 2
            next_tick = time.monotonic() + tick_interval
        if extra_tick is not None and time.monotonic() >= next_extra:
            if extra_tick() == 2:
                return 2
            next_extra = time.monotonic() + (
                extra_tick_interval or tick_interval)
        delay = max(0.0, min(next_scan, next_tick, next_extra)
                    - time.monotonic())
        if stop_event.wait(delay):
            break
    return 0
