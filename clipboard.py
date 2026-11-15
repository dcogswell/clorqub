"""System clipboard via local tools; OSC 52 alone silently no-ops on
many terminals (VTE/GNOME). Text goes over stdin, never argv."""

import os
import subprocess


def copy_text(text, timeout=5):
    """Copy `text` to the desktop clipboard. Returns the tool used or None."""
    candidates = []
    if os.environ.get("WAYLAND_DISPLAY") or \
            os.environ.get("XDG_SESSION_TYPE") == "wayland":
        candidates.append(["wl-copy"])
    if os.environ.get("DISPLAY"):
        candidates += [["xclip", "-selection", "clipboard"],
                       ["xsel", "--clipboard", "--input"]]
    candidates += [["wl-copy"], ["xclip", "-selection", "clipboard"]]
    seen = set()
    for argv in candidates:
        if argv[0] in seen:
            continue
        seen.add(argv[0])
        try:
            # no capture: xclip/xsel daemonize a helper that would hold
            # our pipes open past success -> false timeout
            cp = subprocess.run(argv, input=text, text=True,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if cp.returncode == 0:
            return argv[0]
    return None
