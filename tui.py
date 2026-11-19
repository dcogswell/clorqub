#!/usr/bin/env python3
"""clorqub marketplace viewer (Textual TUI).

Shows the latest scanner snapshot in the local SQLite db written by
`python3 cli.py scan`; the dashboard itself never calls the API. Pressing
`b` on a listing runs the guarded rental flow in-process (live quote ->
in-TUI review -> explicit Confirm & Rent); pressing `l` on a Fleet row
does one explicit read-only SSH fetch of the miner log tail. No other
SSH, no writes to rigs, no auto-confirm.
"""

import argparse
import json
import math
import os
import re
import sys
from datetime import datetime, timezone

try:
    from rich.text import Text
    from textual import work
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical, VerticalScroll
    from textual.screen import ModalScreen
    from textual.scrollbar import ScrollBar
    from textual.widget import Widget
    from textual.widgets import (Button, Checkbox, DataTable, Footer,
                                 Input, Select, Static, TabbedContent,
                                 TabPane, TextArea)
    from textual.widgets._text_area import Selection
except ImportError:
    print(
        "error: the 'textual' package is required for the TUI.\n"
        "Install it without touching the system Python:\n"
        "  python3 -m venv .venv\n"
        "  .venv/bin/pip install -r requirements-tui.txt\n"
        "then run: .venv/bin/python tui.py --db PATH",
        file=sys.stderr,
    )
    raise SystemExit(2)

import clipboard  # noqa: E402
import rental_policy  # noqa: E402
from cli import DEFAULT_DB, DEFAULT_ENV  # noqa: E402
from market import available  # noqa: E402
from formatting import rel_percent  # noqa: E402
from tui_data import (DashboardError, read_dashboard,  # noqa: E402
                      read_log_archive, read_server_details)

CSS = """
$bg: #16181c;
$panel: #1e2228;
$edge: #2dd4bf;
$warn: #f59e0b;
Screen { background: #16181c; }
#hdr { height: 3; padding: 0 1; background: #1e2228; }
#h1 { color: #2dd4bf; text-style: bold; }
#h1 .ro { color: #f59e0b; }
#h2, #h3 { color: #9aa4b0; height: 1; }
#h3 .warn { color: #f59e0b; }
#banner { height: 2; padding: 0 1; color: #f59e0b; display: none; }
#banner.show { display: block; }
#search { height: 3; padding: 0 1; }
TabbedContent { height: 1fr; }
TabPane { padding: 0; }
DataTable { height: 1fr; scrollbar-size-horizontal: 2;
            scrollbar-color: #2dd4bf; scrollbar-color-active: #f59e0b; }
.twrap { height: 1fr; }
Button.nudge { width: 4; min-width: 4; height: 1fr;
               border: none; background: $panel; color: $edge; }
Button.nudge:hover { background: #272c33; }
Button.nudge:focus { text-style: bold; }
#exp-log { height: 60%; }
#exp-upgrade { color: #9aa4b0; height: auto; padding: 0 1; }
.compact #search { display: none; }
.compact #hdr { height: 2; }
.compact #h3 { display: none; }
.compact Button.nudge { width: 3; min-width: 3; }
ServerDetail, HelpModal, OrderDetail, FleetDetail, RentalSetup,
ReviewModal, ProgressModal, LogModal, RenewModal {
    align: center middle; }
#rental .rfield { height: 3; }
#rental #st-gates.gate-off { opacity: 45%; }
#rental #r-error { color: #f59e0b; height: auto; }
#rental Horizontal { height: auto; }
#detail, #rental { width: 90%; height: 90%; background: #1e2228;
          border: solid #2dd4bf; padding: 0 1; }
#rental { width: 90%; max-width: 76; height: 90%; }
#review, #progress, #logview { width: 90%; height: 90%;
          background: #1e2228; border: solid #2dd4bf; padding: 0 1; }
#progress #pstatus { color: #f59e0b; height: auto; }
#logview #lstatus { color: #9aa4b0; height: auto; }
#logview VerticalScroll { height: 1fr; }
#detail .dtitle { color: #2dd4bf; text-style: bold; height: 1; }
"""

def age_of(captured_at, now=None):
    if not captured_at:
        return "unknown age"
    now = now or datetime.now(timezone.utc)
    try:
        dt = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
    except ValueError:
        return "unknown age"
    secs = max(0, (now - dt).total_seconds())
    if secs < 120:
        return f"{secs:.0f}s old"
    if secs < 7200:
        return f"{secs / 60:.0f}m old"
    if secs < 172800:
        return f"{secs / 3600:.1f}h old"
    return f"{secs / 86400:.1f}d old"


def _ep_header(phase):
    """EP header fragment: projected cost of the highest-net-profit
    remaining-epoch fleet plan plus the extra phase budget it would
    need. The runner supplies these already formatted; an absent
    additional (older runner) just shows the cost."""
    plan = phase.get("epoch_plan_usd")
    if plan is None:
        return "EP -"
    extra = phase.get("epoch_plan_additional_usd")
    if extra is None:
        return f"EP ~${plan}"
    return f"EP ~${plan} (+${extra})"


def plain(value):
    return Text("" if value is None else str(value))


def _dim(cell):
    cell.stylize("dim")
    return cell


CAND_COLS = ("ID", "GPU", "OD$/d", "Mode", "Est$/h", "Net$/h", "EstRet",
             "Auto", "Upg", "est it/s", "Spot%", "CC",
             "thr", "CPU model", "ISA", "Rel%")
MKT_COLS = ("ID", "GPU", "CPU", "Price$/d", "SpotFlr", "Avail", "CC",
            "thr", "CPU model", "ISA", "Rel%",
            "Reason")
EXP_COLS = ("Order", "Server", "Role", "Mode", "Tier", "CPU", "GPU",
            "$/hr", "it/s", "EstRet", "Gross$/h", "Net$/h", "Age",
            "Shares", "Sh/h",
            "Last share", "Tree", "Next")
FLEET_COLS = ("Server", "Pool", "Health", "CPU it/s", "GPU it/s",
              "Epoch", "Found", "Age", "$/hr", "Sh/h", "Sh/¢", "Spend$",
              "Online", "Control", "Mode", "Up",
              "End", "Order", "GPU", "Worker", "Submitted", "Trainer")

# app actions that only make sense on some tabs -> the footer hides
# (and disables) them elsewhere via check_action
PANE_ACTIONS = {
    "toggle_available": ("candidates", "marketplace"),
    "toggle_sort": ("candidates", "marketplace"),
    "rent_selected": ("candidates", "marketplace"),
    "toggle_fleet_all": ("fleet",),
    "log_selected": ("fleet", "experiment"),
}


def age_s_label(age_s):
    """'2h14m' from a duration in seconds; '-' when unknown."""
    if age_s is None:
        return "-"
    return uptime_label(datetime.fromtimestamp(
        datetime.now(timezone.utc).timestamp() - age_s,
        timezone.utc).isoformat())


def end_label(deadline_at):
    """Local rental end: 'HH:MM' today, 'MM-DD HH:MM' otherwise."""
    if not deadline_at:
        return "-"
    try:
        end = datetime.fromisoformat(
            deadline_at.replace("Z", "+00:00")).astimezone()
    except (ValueError, TypeError):
        return trunc(deadline_at, 14)
    now = datetime.now()
    return end.strftime("%H:%M" if end.date() == now.date()
                        else "%m-%d %H:%M")


def uptime_label(created_at, until=None):
    """Compact rental uptime '2h14m' / '3d5h'; '-' when unknown."""
    if not created_at:
        return "-"
    try:
        start = datetime.fromisoformat(
            created_at.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return "-"
    end = datetime.now(timezone.utc)
    if until:
        try:
            end = min(end, datetime.fromisoformat(
                until.replace("Z", "+00:00")).astimezone(timezone.utc))
        except (ValueError, TypeError, AttributeError):
            pass
    secs = max(0, int((end - start.astimezone(timezone.utc))
                      .total_seconds()))
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    if days:
        return f"{days}d{hours}h"
    if hours:
        return f"{hours}h{mins:02d}m"
    return f"{mins}m"


def rig_gpu(rig):
    """First GPU name, agent-reported then order record; compact label."""
    gpus = ((rig.get("status") or {}).get("hardware") or {}).get("gpus") \
        or rig.get("order_gpus") or []
    if not gpus:
        return "?"
    name = re.sub(r"(?i)^\s*gpu\s*\d+\s*:", "", str(gpus[0]))
    name = re.sub(r"(?i)\s*\(uuid:[^)]*\)", "", name)
    name = re.sub(r"(?i)\b(nvidia|geforce)\b", "",
                  name).strip() or str(gpus[0])
    return trunc(name, 12) + ("+" if len(gpus) > 1 else "")


def spot_floor_label(row):
    """Fee-inclusive spot floor, '*' if offers seen; '-' missing/stale."""
    s = row.get("spot") or {}
    floor = s.get("fee_inclusive_floor_usd_day")
    if floor is None:
        return "-"
    return f"{floor:.2f}" + ("*" if s.get("offers_count") else "")


def gpu_label(row):
    """Score-normalized GPU, else the raw listing gpu_array for display."""
    gpu = row.get("gpu")
    if gpu:
        return gpu
    arr = ((row.get("listing") or {}).get("gpu_array")) or []
    return ", ".join(str(g) for g in arr) or "?"


def fmt_price(row):
    price = row.get("effective_price_day_usd")
    return f"{price:.2f} ({price / 24:.3f}/h)" if price is not None else "-"


def fmt_rel(row):
    return rel_percent(row.get("reliability"))


def _detail_val(v):
    if isinstance(v, (list, tuple)):
        return ",".join(str(x) for x in v)
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


_EVENT_REASONS = {
    "one_rent_per_cycle": "deferred: 1 rental per cycle",
    "telemetry_stale": "rig telemetry stale",
    "stale_market": "market snapshot too old",
    "phase": "phase ended",
    "pending": "manual rent attempt in flight",
    "budget": "phase budget exhausted",
    "probe_guard": "probe near free-window deadline",
    "balance": "wallet balance too low",
    "cancel_retry_backoff": "cancel retry backoff",
    "blacklisted": "server blacklisted",
    "probe_deadline": "probe hit free-window deadline",
    "unreachable": "rig unreachable",
    "dry": "never produced shares",
    "dry_after_x": "still dry after its x_min arm",
    "replaced": "replaced by probe winner",
    "upgraded": "beaten by upgrade probe",
    "displaced": "displaced by retry-burst winner",
    "term_end": "rental term ended",
    "startup": "no shares during startup",
    "zero_its": "zero it/s",
    "probe_lost": "lost probe audition",
    "probe_no_share": "probe never shared",
    "provision_failed": "provisioning failed",
    "flaky_ssh": "flaky ssh",
    "cancelled": "cancelled",
    "missing": "order missing",
    "observe-only": "observe-only",
    "rerent": "scheduled re-rent",
    "fill": "slot fill",
    "probe": "burst probe",
    "manual": "manual request",
    "upgrade": "upgrade audition",
    "dry_retry": "dry-slot retry",
    "outbid": "outbid on spot market",
    "paused": "order paused by market",
    "reactivate_blocked": "resumed spot order can't be carried",
    "reactivated": "resumed spot order audition",
}


def _localize(dt):
    return dt.astimezone() if dt.tzinfo is not None else dt


def _short_ts(v):
    """ISO timestamp -> local HH:MM when it parses; raw value
    otherwise."""
    try:
        return _localize(datetime.fromisoformat(
            str(v).replace("Z", "+00:00"))).strftime("%H:%M")
    except (ValueError, TypeError):
        return str(v or "-")


def _reason_text(reason):
    """Jargon reason -> short plain phrase; unknown values pass
    through so nothing is ever hidden."""
    if reason is None:
        return ""
    r = str(reason)
    if r.startswith("fault:"):
        return "fault: " + r.split(":", 1)[1]
    if r.startswith("balance:"):
        return "wallet check failed: " + r.split(":", 1)[1].strip()
    return _EVENT_REASONS.get(r, r)


def _ev_line(e):
    """One experiment-log line: HH:MM:SS + a plain-English sentence
    for the event, then any detail fields the sentence didn't use
    (flattened, truncated) so no data is ever dropped."""
    try:
        hhmmss = _localize(datetime.fromisoformat(
            str(e["ts"]).replace("Z", "+00:00"))).strftime("%H:%M:%S")
    except (ValueError, TypeError):
        hhmmss = str(e.get("ts") or "")[:8]
    kind = e.get("kind") or "?"
    detail = e.get("detail") or {}
    used = {"server_id", "order_id"}

    def take(k):
        used.add(k)
        return detail.get(k)

    oid = e.get("order_id")
    sid = e.get("server_id")
    who = (f" #{oid}" if oid is not None else "") + \
          (f" s{sid}" if sid is not None else "")
    if kind == "note":
        body = "note " + str(take("text") or "")
    elif kind == "tick":
        rigs = take("rigs")
        take("pulse")
        body = (f"alive — committed=${take('committed')} "
                f"remaining=${take('remaining')} "
                f"burn=${take('burn_hr')}/h "
                f"opt=${take('optimal_hr') or '-'}/h"
                + (f"  |  {rigs}" if rigs else ""))
    elif kind == "rent":
        body = (f"rent{who} {take('tier')}/"
                f"{take('role') or 'slot'} ({take('reason')})")
    elif kind in ("blocked", "cancel", "restart", "ended"):
        body = (f"{kind}{who}: "
                f"{_reason_text(take('reason'))}")
    elif kind == "first_share":
        age = take("age_min")
        body = (f"first share{who}"
                + (f" after {age}m" if age is not None else ""))
    elif kind == "blacklisted":
        body = (f"blacklist{who}: {_reason_text(take('reason'))}"
                f" ({take('ttl_days')}d)")
    elif kind == "burst_started":
        body = (f"burst #{take('burst_id')} started"
                f" ({take('trigger')} n={take('n')}"
                f" y={take('y_min')}m)")
    elif kind == "burst_judged":
        extra = take("promoted_extra") or []
        body = (f"burst #{take('burst_id')} judged:"
                f" {take('verdict')}"
                + (f" +{len(extra)} kept"
                   + "".join(f" o{e['order_id']}:{e.get('via')}"
                             for e in extra) if extra else ""))
    elif kind == "burst_scheduled":
        body = (f"retry burst for #{take('for_order')}"
                f" due {_short_ts(take('due_at'))}")
    elif kind == "burst_requested":
        body = f"burst requested n={take('n')}"
    elif kind == "rerent_scheduled":
        body = f"rerent{who} due {_short_ts(take('due_at'))}"
    elif kind in ("rerent_expired", "retry_expired", "retry_cleared"):
        body = f"{kind}{who}"
    elif kind == "dryrun":
        reason = take("reason")
        body = (f"dry-run{who} {take('type')}"
                + (f" ({_reason_text(reason)})" if reason else ""))
    elif kind == "rental_value":
        shares = take("shares") or 0
        weighted = take("weighted")
        usd = take("usd_est")
        cost = take("cost_usd")
        ret = take("return_x")
        method = take("method")
        alloc = ("wallet-average allocation"
                 if method == "wallet_average"
                 else "legacy allocation")
        body = (f"value estimate{who}: {shares:,.0f} shares -> "
                f"{(weighted or 0):,.0f} weighted ({alloc})"
                + (f" ~ ${usd:.2f}" if usd is not None else "")
                + (f" vs ${cost:.2f} cost" if cost is not None else "")
                + (f" ({ret:.1f}x)" if ret is not None else ""))
    elif kind == "paused":
        body = (f"paused{who}: Clore reports the order not running"
                f" ({take('mode') or 'on-demand'})")
    elif kind == "reactivated":
        body = (f"reactivated{who}: spot order resumed after "
                f"{take('paused_min')}m paused"
                f" (burst #{take('burst_id')})")
    else:
        body = f"{kind}{who}"
    flat = {}
    for k, v in detail.items():
        if isinstance(v, dict):
            flat.update(v)
        else:
            flat[k] = v
    rest = " ".join(
        f"{k}={trunc(_detail_val(v), 28)}"
        for k, v in flat.items()
        if v is not None and k not in used)
    return f"{hhmmss} {body}{('  ' + rest) if rest else ''}"


def trunc(value, width):
    """Display-only truncation; raw values stay intact in detail."""
    text = str(value or "")
    return text if len(text) <= width else text[:width - 1] + "~"


CPU_STRIP = re.compile(r"\b(?:AMD|Intel|Processor)\b|\(R\)|\(TM\)|-Core\b")


CC_NAMES = {
    "AE": "United Arab Emirates", "AM": "Armenia",
    "AR": "Argentina", "AT": "Austria", "AU": "Australia",
    "BE": "Belgium", "BG": "Bulgaria", "BR": "Brazil",
    "BY": "Belarus", "CA": "Canada", "CH": "Switzerland",
    "CL": "Chile", "CN": "China",
    "CO": "Colombia", "CZ": "Czechia", "DE": "Germany",
    "DK": "Denmark", "DZ": "Algeria", "EE": "Estonia",
    "EG": "Egypt", "ES": "Spain",
    "FI": "Finland", "FR": "France", "GB": "United Kingdom",
    "GE": "Georgia",
    "GR": "Greece", "HK": "Hong Kong", "HR": "Croatia",
    "HU": "Hungary", "ID": "Indonesia", "IE": "Ireland",
    "IL": "Israel", "IN": "India", "IS": "Iceland", "IT": "Italy",
    "IR": "Iran", "JP": "Japan", "KR": "South Korea",
    "KZ": "Kazakhstan", "LT": "Lithuania", "LU": "Luxembourg",
    "LV": "Latvia", "LY": "Libya", "MD": "Moldova",
    "MX": "Mexico", "MY": "Malaysia", "NL": "Netherlands",
    "NO": "Norway", "NZ": "New Zealand", "PH": "Philippines",
    "PL": "Poland", "PT": "Portugal",
    "RO": "Romania", "RS": "Serbia", "RU": "Russia",
    "SA": "Saudi Arabia", "SE": "Sweden",
    "SG": "Singapore", "SI": "Slovenia", "SK": "Slovakia",
    "TH": "Thailand", "TR": "Turkey", "TW": "Taiwan",
    "UA": "Ukraine", "US": "United States", "VN": "Vietnam",
    "ZA": "South Africa",
}


def cc_name(cc):
    """ISO alpha-2 -> country name; falls back to the raw code."""
    return CC_NAMES.get(str(cc or "").upper()) or cc


def isa_label(isa):
    """Compact ISA: '512' / '512~'(inferred), 'avx2', '-'."""
    text = str(isa or "")
    if "AVX512" in text:
        return "512~" if "inferred" in text else "512"
    if "AVX2" in text:
        return "avx2~" if "inferred" in text else "avx2"
    return "-"


def cpu_compact(cpu):
    """Short CPU label for the table; full model stays in detail."""
    text = CPU_STRIP.sub("", str(cpu or ""))
    return trunc(" ".join(text.split()) or "?", 16)


def rate_fmt(value):
    """Unknown -> '-'; explicit zero -> '0.0' (never summed)."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "-"
    return f"{value:.1f}"


_DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)([dhms])")
_END_LABEL = re.compile(r"(?:(\d{2})-(\d{2}) )?(\d{2}):(\d{2})")


def _cell_sort_key(cell, reverse=False):
    """Text cell -> (rank, value): numbers/durations first, then
    strings; '-', '?' and 'n/a' always last -- reverse is folded into
    the key so descending never floats the placeholders up top."""
    text = cell.plain if isinstance(cell, Text) else str(cell)
    s = text.strip().lower()
    if not s or s in ("-", "?", "n/a", "unknown age"):
        return (2, 0)
    val = None
    m = _END_LABEL.fullmatch(s)
    if m:
        now = datetime.now()
        mon, day = ((int(m.group(1)), int(m.group(2)))
                    if m.group(1) else (now.month, now.day))
        val = datetime(now.year, mon, day, int(m.group(3)),
                       int(m.group(4)), tzinfo=timezone.utc).timestamp()
    elif _DURATION_PART.match(s):
        val = sum(float(v) * {"d": 86400, "h": 3600, "m": 60,
                              "s": 1}[u]
                  for v, u in _DURATION_PART.findall(
                      s.removesuffix(" old")))
    else:
        try:
            val = float(re.sub(r"[^\d.\-]", "", s.split()[0]))
        except ValueError:
            pass
    if val is not None:
        return (0, -val if reverse else val)
    return (1, tuple(-ord(c) for c in s) + (math.inf,)
            if reverse else s)


class ServerDetail(ModalScreen):
    """Scrollable per-server modal: overview + spec/price history."""

    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("q", "close", "Close"),
        Binding("j", "toggle_json", "Raw JSON"),
    ]

    def __init__(self, detail, dash_row):
        super().__init__()
        self.detail = detail
        self.row = dash_row
        self.show_json = False

    def compose(self) -> ComposeResult:
        with Vertical(id="detail"):
            yield Static(
                f"server {self.detail['server_id']}  "
                f"owner {self.detail['owner']}  READ ONLY",
                classes="dtitle")
            with TabbedContent():
                with TabPane("Overview"):
                    yield VerticalScroll(Static(self._overview(), id="ov"))
                with TabPane("Hardware History"):
                    yield VerticalScroll(Static(self._specs()))
                with TabPane("Price History"):
                    yield VerticalScroll(Static(self._prices()))
                with TabPane("Rentals"):
                    yield VerticalScroll(Static(self._rentals()))
            yield Horizontal(
                Button("Raw JSON", id="sd-json"),
                Button("Close", id="sd-close", variant="primary"))

    def on_button_pressed(self, event):
        if event.button.id == "sd-json":
            self.action_toggle_json()
        else:
            self.dismiss()

    def _kv(self, label, value):
        return f"{label:<14} {value}"

    def _rentals(self):
        rentals = self.detail.get("rentals") or []
        if not rentals:
            return ("no rentals recorded for this server "
                    "(only clorqub-tracked orders appear here)")
        lines = [
            f"rental history ({len(rentals)}):", "",
            f"{'order':<9} {'pool':<7} {'state':<16} {'up':<7} "
            f"{'$/hr':<7} {'spend':<7} {'sh/h':<7} renew",
        ]
        for r in rentals:
            rate = (f"{r['rate_hr_usd']:.3f}"
                    if r.get("rate_hr_usd") is not None else "-")
            spend = (f"{r['spend_usd']:.2f}"
                     if r.get("spend_usd") is not None else "-")
            shr = (f"{r['shares_hr']:.0f}"
                   if r.get("shares_hr") is not None else "-")
            lines.append(
                f"{r.get('order_id'):<9} "
                f"{str(r.get('pool') or '?')[:7]:<7} "
                f"{str(r.get('provision_state') or '?')[:16]:<16} "
                f"{uptime_label(r.get('created_at'), r.get('up_until')):<7} "
                f"{rate:<7} {spend:<7} {shr:<7} "
                f"{'ON' if r.get('auto_renew') else ''}")
        return "\n".join(lines)

    def _overview(self):
        d, r = self.detail, self.row
        spot = d.get("spot") or {}
        specs = (d["listing"].get("specs") or {})
        entry = r.get("blacklist")
        lines = ([f"BLACKLIST: {entry['reason']} ({entry['source']})",
                  f"added {entry['added_at']}; expires "
                  f"{entry['expires_at'] or 'never'}", ""] if entry else []) + [
            self._kv("server id", d["server_id"]),
            self._kv("owner", d["owner"]),
            self._kv("country", cc_name(d["country"]) or "--"),
            self._kv("first seen", d["first_seen"]),
            self._kv("last seen", d["last_seen"]),
            self._kv("listed cpu", specs.get("cpu") or "Unknown CPU"),
            self._kv("threads", specs.get("cpus") or "?"),
            self._kv("gpu", ", ".join(str(g) for g in
                                      (d["listing"].get("gpu_array") or []))
                     or "?"),
            self._kv("vram GB", specs.get("gpuram")),
            self._kv("cuda", d["listing"].get("cuda_version")),
            self._kv("ram GB", specs.get("ram")),
            self._kv("net", cc_name((specs.get("net") or {}).get("cc"))),
            self._kv("reliability",
                     rel_percent(d["listing"].get("reliability"))),
            "               (Clore-reported server reliability;",
            "                informational, not mining yield/scoring)",
            self._kv("rented", d["listing"].get("rented")),
            self._kv("price $/day", (d["listing"].get("price") or {})
                     .get("usd", {}).get("on_demand_usd")),
            self._kv("reason", r.get("ineligible_reason") or "eligible"),
            "",
            "uncertainty:",
            f"  {r.get('reference_uncertainty') or 'unknown'}",
            "",
            "trainer/epoch versions are not recorded for listings; "
            "treat them as unknown.",
            "",
            "spot floor (observed, indicative only):",
            f"  status    {spot.get('status') if spot else 'not collected'}",
            f"  captured  {spot.get('captured_at') if spot else '-'}",
            f"  floor     {(spot.get('floor_usd_day') if spot else None)} "
            "USD/day",
            f"  fee-incl  {(spot.get('fee_inclusive_floor_usd_day') if spot else None)} "
            "USD/day (1.25% assumed)",
            f"  offers    {(spot.get('offers_count') if spot else None)}",
            f"  note      {(spot.get('note') if spot else '-')}",
            "",
            "press j or tap Raw JSON for raw listing JSON",
        ]
        if self.show_json:
            lines += ["", json.dumps(d["listing"], indent=2, sort_keys=True)]
        return Text("\n".join(lines))

    def _specs(self):
        lines = ["newest 20 observed spec intervals",
                 "--------------------------------"]
        for i, s in enumerate(self.detail["specs"], 1):
            valid_to = s.get("valid_to") or "current"
            lines.append(
                f"[{i}] {s.get('observed_at')}  ->  {valid_to}")
            lines.append(
                f"    cpu={s.get('cpu_model')}  offer={s.get('cpu_offer')}"
                f"  vram={s.get('vram_gb')}  cuda={s.get('cuda_version')}"
                f"  ram={s.get('ram_gb')}")
            lines.append(f"    gpu={s.get('gpu_array_json')}")
        if not self.detail["specs"]:
            lines.append("(no spec history recorded)")
        return Text("\n".join(lines))

    def _prices(self):
        lines = ["newest 20 observed price intervals",
                 "---------------------------------"]
        for i, p in enumerate(self.detail["prices"], 1):
            valid_to = p.get("valid_to") or "current"
            lines.append(
                f"[{i}] {p.get('observed_at')}  ->  {valid_to}"
                f"  on-demand ${p.get('on_demand_usd_day')}/day")
        if not self.detail["prices"]:
            lines.append("(no price history recorded)")
        return Text("\n".join(lines))

    def action_close(self):
        self.dismiss()

    def action_toggle_json(self):
        self.show_json = not self.show_json
        self.query_one("#ov", Static).update(self._overview())


HELP_TEXT = """keys
----
  /            reveal + focus search filter (Enter/Escape hides the bar
               again on any screen size; the filter still applies)
  b            rent setup -> in-TUI quote review -> Confirm & Rent
             (no terminal needed; nothing spends until you confirm)
  l            (on Fleet or Experiment) tail the rig's miner log -- one
             explicit read-only SSH fetch, no polling; ended rigs show
             the locally archived copy instead
  a            toggle available-only
  s            toggle sort: forecast net / value desc <-> $/day asc
  f            toggle fleet view: active rigs <-> all (incl. ended)
  c            reset filters (search, availability, sort, fleet view)
  r            reload the local SQLite db (rereads the db file; NOT an
             API refresh -- this app never calls the Clore API)
  Enter        open server detail on the highlighted row
  j            (in detail) toggle raw listing JSON
  Escape/q     close detail or this help (Close button works too)
  q            quit

data
----
  db: {db_path}
  View of the latest CACHED snapshot the scanner wrote. Rows,
  anchors and warnings are provisional. The dashboard itself never
  calls the Clore API. `b` runs rent.py's guarded flow in-process:
  a live quote, an in-TUI review, then your explicit Confirm & Rent
  click -- the same fresh-GET re-verification, one-POST and
  auto-cancel-on-bad-receipt rules as the CLI. `l` on a Fleet row
  does ONE read-only SSH fetch of the miner log tail. No other SSH,
  no writes to rigs, no auto-confirm anywhere.
  Header EP: projected cost of the highest predicted NET-profit
  gated fleet for the rest of the epoch (not highest ROI, not a
  payout guarantee); (+$N) is the extra phase budget the plan
  would need -- $0 means already funded, '-' means no fresh pool
  calibration, and 'EP needs phase extension' means the projection
  outlives the current phase deadline (which stays unchanged).
  Experiment tab: it/s is the runner's smoothed measurement;
  Gross$/h is forecast revenue before cost (the gross-strategy
  ranking metric); Net$/h and EstRet are provisional forecasts
  -- an incumbent's creation fee is sunk (excluded) while a
  probe's includes a fresh fee over its remaining term. The
  pinned 'Latest upgrade
  check' is the newest dated 'upgrade scan:' note -- forecast
  net $/h against the configured 25% profit-improvement
  threshold (relative value/$ without a pool model) -- not a
  live ranking while a burst is in progress.

collector (run these in a shell, not here)
------------------------------------------
  python3 collector.py start    # scans every 5 min
  python3 collector.py status   # is it running?
  python3 collector.py logs     # recent scan log
  python3 collector.py stop
  Foreground: cli.py scan --live --watch

experiment
----------
  The Experiment tab is a read-only view of experiment.py's tables:
  phase budget/committed/remaining, each rental's role (slot, probe,
  kept, control), and a Next column showing the runner's soonest
  upcoming action (startup/dry restart, gives-up deadline, judge or
  cancel deadline, term end). `runner:` shows whether `experiment.py
  run` holds its lock. The event tail below the table mirrors the
  runner's log (restart/cancel/rerent/burst decisions). Start or stop
  the runner from a terminal (`experiment.py start-phase` /
  `experiment.py run`), never from here -- this app never creates or
  cancels orders. Enter on a rental row opens its Fleet detail.

spot floors
-----------
  SpotFlr is an observed fee-inclusive floor (USD-Blockchain, 1.25%
  fee assumed), indicative only -- never a winning bid. Opt in with
  --spot-limit N (0..10); ranking stays on-demand.
  On Candidates, Spot% is the discount that quote implies vs the
  on-demand price and Mode 'spot' marks rows the runner would bid
  on; both need a live phase with spot_enabled and a fresh quote.
  Price$/d and OD$/d are the fee-inclusive ON-DEMAND daily price
  (5% renter fee assumed, not refreshed).

fleet
-----
  The Fleet tab shows rigs you enrolled via `rent.py create`/`enroll`.
  A watcher SSH-reads each enrolled rig every 30 s (separate from the
  5-min Clore API scans) and stores sanitized status; unreachable rigs
  keep last-good rates marked stale. CPU/GPU it/s are independent,
  never summed. Share counters are agent-cumulative. `l` on a rig
  fetches the tail of its miner log once, on demand; if the live
  fetch fails the locally archived copy is shown instead. Rental control
  is the guarded `b` flow here or `rent.py` in a shell.
  Sh/¢ is shares/hour per fee-inclusive cent/hour of rental cost.
  Spend$ is Clore's reported cumulative spend and Online its reported
  online flag (not verified), from the last successful Clore poll. Control: experiment (rented by
  experiment.py), managed (rent.py/TUI), enrolled (observe-only) or
  unmanaged.
  Clore orders never enrolled as rigs (manual web-UI rentals, etc.)
  appear as 'unmanaged' rows with '-' telemetry cells -- Enter shows
  their reported order record.

blacklist
---------
  Automatic server blocks expire after 3 days. The experiment runner
  flags fault/unreachable cancellations, failed provisioning, >40% failed
  reads over 30 minutes (20+ samples), or CPU/allocation mismatch.
  Dry trees never count.
  Blocks prevent automatic rental; manual rental shows a warning.
  python3 blacklist.py list [--all]
  python3 blacklist.py add --server ID --reason TEXT
  python3 blacklist.py rm --server ID
  Manual entries never expire. Add --db PATH to use another database."""


class HelpModal(ModalScreen):
    """Scrollable plain-text help; q/Escape closes (never quits app)."""

    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("q", "close", "Close"),
    ]

    def __init__(self, db_path):
        super().__init__()
        self.db_path = db_path

    def compose(self) -> ComposeResult:
        with Vertical(id="detail"):
            yield Static("cq marketplace help  READ ONLY",
                         classes="dtitle")
            yield VerticalScroll(Static(Text(
                HELP_TEXT.format(db_path=self.db_path))))
            yield Horizontal(
                Button("Close", id="help-close", variant="primary"))

    def on_button_pressed(self, event):
        self.dismiss()

    def action_close(self):
        self.dismiss()


class OrderDetail(ModalScreen):
    """Read-only order detail plus a Rent again shortcut for its
    server -- opens the same guarded RentalSetup flow as `b`."""

    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("q", "close", "Close"),
    ]

    def __init__(self, order):
        super().__init__()
        self.order = order

    def compose(self) -> ComposeResult:
        o = self.order
        shr = (f"{o['shares_hr']:.1f}"
               if o.get("shares_hr") is not None else "-")
        spc = (f"{o['shares_per_cent']:.2f}"
               if o.get("shares_per_cent") is not None else "-")
        kind = ("spot" if o.get("spot") is True else
                "on-demand" if o.get("spot") is False else "unknown")
        lines = [
            f"order {o.get('order_id')}  READ ONLY",
            f"server id        {o.get('server_id')}",
            f"type             {kind}",
        ]
        if o.get("state"):
            lines.append(
                f"state            {o['state']}  "
                f"first seen {o.get('first_seen')}  "
                f"last seen {o.get('last_seen')}")
        lines += [
            f"online reported  {o.get('online_reported')} "
            "(not proven miner-running)",
            f"currency         {o.get('currency')}",
            f"price/day        {o.get('price_day_native')} (native)",
            f"spend reported   {o.get('spend_reported_native')} (native, "
            "cumulative -- not epoch spend, not fee-verified)",
            f"shares/hr        {shr} (agent telemetry; '-' if not "
            "enrolled)",
            f"shares/¢         {spc} (shares per fee-inclusive cent "
            "of spend rate)",
            f"creation fee     {o.get('quoted_creation_fee_native')} "
            "(quote, not proof charged)",
            f"created (unix)   {o.get('created_unix')}",
            f"cpu              {o.get('cpu')}",
            f"gpus             {', '.join(o.get('gpus') or [])}",
            f"control          {o.get('control')}",
        ]
        with Vertical(id="detail"):
            yield VerticalScroll(Static(Text("\n".join(lines))))
            yield Horizontal(
                Button("Rent again", id="od-rent", variant="warning"),
                Button("Close", id="od-close", variant="primary"))

    def on_button_pressed(self, event):
        if event.button.id == "od-close":
            self.dismiss()
        elif event.button.id == "od-rent":
            server_id = self.order.get("server_id")
            self.dismiss()
            if server_id is not None:
                self.app._rent_server(server_id)

    def action_close(self):
        self.dismiss()


class FleetDetail(ModalScreen):
    """Read-only rig detail; telemetry is sanitized agent output only."""

    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("q", "close", "Close"),
    ]

    def __init__(self, rig, db_path):
        super().__init__()
        self.rig = rig
        self.db_path = db_path

    def _ssh_cmd(self):
        r = self.rig
        if not r.get("ssh_host") or not r.get("ssh_port"):
            return None
        return (f"ssh -i {r.get('ssh_key_path')} -p {r.get('ssh_port')}"
                f" -o UserKnownHostsFile=~/.config/clorqub/known_hosts-"
                f"{r.get('order_id')} root@{r.get('ssh_host')}")

    def _kv(self, label, value):
        return f"{label:<15} {value if value not in (None, '') else '-'}"

    def compose(self) -> ComposeResult:
        r = self.rig
        st = r.get("status") or {}
        miner = st.get("miner") or {}
        its = st.get("its") or {}
        shares = st.get("shares") or {}
        trainer = st.get("trainer") or {}
        package = st.get("package") or {}
        hw = st.get("hardware") or {}
        devices = its.get("devices") or {}
        lines = [
            f"order {r.get('order_id')}  READ ONLY",
            self._kv("server id", r.get("server_id")),
            self._kv("country", cc_name(r.get("country"))),
            self._kv("health", r.get("health")),
            self._kv("registered", r.get("alias")),
            self._kv("worker", st.get("alias")),
            self._kv("pool asked", r.get("pool")),
            self._kv("pool live", st.get("pool")),
            self._kv("provision", r.get("provision_state")),
            self._kv("error", r.get("error_kind")),
            self._kv("ssh", f"{r.get('ssh_host')}:{r.get('ssh_port')}"
                     if r.get("ssh_host") else "-"),
            self._kv("ssh cmd", self._ssh_cmd() or "-"),
            self._kv("agent UTC", st.get("updated_at")),
            self._kv("observed", r.get("observed_at")),
            self._kv("last try", r.get("attempted_at")),
            self._kv("term end", r.get("end_at")),
            self._kv("auto-renew", "ON" if r.get("auto_renew") else
                     ("off" if r.get("auto_renew") == 0 else "-")),
            "",
            "rates (independent; never summed):",
            self._kv("  cpu it/s", its.get("cpu")),
            self._kv("  gpu it/s", its.get("gpu")),
            self._kv("  devices", ", ".join(
                f"{k}={v}" for k, v in devices.items()
                if v is not None) or "-"),
            "",
            "share counters (agent-reported cumulative -- resets on "
            "miner restart; not per-epoch verified):",
            self._kv("  epoch", shares.get("epoch")),
            self._kv("  found", shares.get("found")),
            self._kv("  submitted", shares.get("submitted")),
            self._kv("  invalid", shares.get("invalid")),
            self._kv("  log f/s/r", "/".join(str(shares.get(k))
                     for k in ("log_found", "log_submitted",
                               "log_rejected"))),
            "",
            "miner:",
            self._kv("  running", miner.get("running")),
            self._kv("  started", miner.get("started_at")),
            "trainer/package:",
            self._kv("  trainer", " ".join(x for x in (
                trainer.get("variant"), trainer.get("flavor"),
                trainer.get("version")) if x) or "-"),
            self._kv("  package", " ".join(x for x in (
                package.get("version"), package.get("sha256")) if x)
                or "-"),
            "hardware:",
            self._kv("  cpu", hw.get("cpu_model")),
            self._kv("  nproc", hw.get("nproc")),
            self._kv("  avx2/avx512", f"{hw.get('avx2')}/"
                     f"{hw.get('avx512f')}"),
            self._kv("  gpus", ", ".join(hw.get("gpus") or []) or "-"),
            "",
            "billing (quoted terms, NOT actual epoch spend):",
            self._kv("  burn $/hr", f"{r['rate_hr_usd']:.3f}"
                     if r.get("rate_hr_usd") is not None else None),
            self._kv("  spent $", f"{r['spend_usd']:.2f}"
                     if r.get("spend_usd") is not None else None),
            self._kv("  deadline", r.get("deadline_at")),
            self._kv("  auto-renew", {1: "on", 0: "off"}.get(
                r.get("auto_renew"))),
            self._kv("  cap $", r.get("max_cost_usd")),
            self._kv("  quoted $", r.get("quoted_cost_usd")),
            "",
            "No miner controls exist here; rental control is via "
            "rent.py in a shell.",
        ]
        with Vertical(id="detail"):
            yield VerticalScroll(Static(Text("\n".join(lines))))
            yield Horizontal(
                Button("Cancel order", id="fd-cancel", variant="error",
                       disabled=r.get("provision_state") in
                       ("observe-only", "cancelled", "missing")),
                Button("Auto-renew", id="fd-renew",
                       disabled=r.get("provision_state") not in
                       ("pending", "ready")),
                Button("Copy SSH cmd", id="fd-copy",
                       disabled=self._ssh_cmd() is None),
                Button("Close", id="fd-close", variant="primary"))

    def on_button_pressed(self, event):
        bid = event.button.id
        if bid == "fd-close":
            self.dismiss()
        elif bid == "fd-cancel":
            self.app.push_screen(
                CancelModal(self.rig, self.db_path),
                lambda _result: self._cancel_returned())
        elif bid == "fd-renew":
            self.app.push_screen(
                RenewModal(self.rig, self.db_path),
                lambda _result: self.app.reload_data())
        else:  # fd-copy
            self.app.copy_to_clipboard(self._ssh_cmd())
            self.app.notify_copied("ssh command copied")

    def _cancel_returned(self):
        try:
            self.query_one("#fd-cancel", Button).disabled = True
        except Exception:
            pass

    def action_close(self):
        self.dismiss()


class CancelModal(ModalScreen):
    """Confirm-then-cancel for a managed rental. ONE cancel_order POST,
    no retry; Escape/Back is display-only once it has been sent."""

    BINDINGS = [Binding("escape", "close", "Back")]

    def __init__(self, rig, db_path):
        super().__init__()
        self.rig = rig
        self.db_path = db_path

    def compose(self) -> ComposeResult:
        r = self.rig
        with Vertical(id="logview"):
            yield Static(
                f"cancel order {r.get('order_id')} -- server "
                f"{r.get('server_id')}  pool {r.get('pool')}  "
                f"state {r.get('provision_state')}", classes="dtitle")
            yield Static(
                "Cancelling stops billing and destroys the rig "
                "immediately; this cannot be undone. One "
                "cancel_order POST is sent -- never retried.")
            yield Static("", id="cstatus")
            yield Horizontal(
                Button("Confirm cancel", id="cc-go", variant="error"),
                Button("Back", id="cc-back", variant="primary"))

    def on_mount(self):
        self._app = self.app

    def on_button_pressed(self, event):
        if event.button.id == "cc-back":
            self.dismiss()
            return
        go = self.query_one("#cc-go", Button)
        go.disabled = True
        go.label = "sending..."
        self._append("sending cancel request -- waiting for API "
                     "(one POST, no retry)...")
        self._run()

    def _append(self, msg):
        try:
            self.query_one("#cstatus", Static).update(Text(msg))
        except Exception:
            pass  # modal already dismissed

    @work(thread=True, exclusive=True)
    def _run(self):
        import rent

        def collect(msg):
            self._app.call_from_thread(self._append, str(msg))

        try:
            rc, info = rent.cancel_managed_order(
                self.rig["order_id"], self.db_path, DEFAULT_ENV,
                say=collect, warn=collect)
        except Exception as exc:
            # last-resort guard -- type name only, never raw text
            rc, info = 1, {"order_id": self.rig.get("order_id"),
                           "stage": type(exc).__name__}
        self._app.call_from_thread(self._finish, rc, info)

    def _finish(self, rc, info):
        try:
            self.query_one("#cstatus", Static).update(Text(
                f"done rc={rc} state={(info or {}).get('state') or '-'}"
                f" stage={(info or {}).get('stage') or '-'}"))
            self.query_one("#cc-back", Button).label = "Close"
        except Exception:
            pass
        order_id = self.rig.get("order_id")
        if rc == 0:
            self._app.notify(f"order {order_id} ended")
        else:
            self._app.notify(
                f"cancel order {order_id} did not complete -- "
                "check the web UI", severity="warning")
        self._app.reload_data()

    def action_close(self):
        self.dismiss()


class RenewModal(ModalScreen):
    """Enable/disable auto-renew on a managed ACTIVE on-demand order.
    ONE set_order_auto_renew POST, no retry; Escape/Back is
    display-only once it has been sent."""

    BINDINGS = [Binding("escape", "close", "Back")]

    def __init__(self, rig, db_path):
        super().__init__()
        self.rig = rig
        self.db_path = db_path
        self.on_now = rig.get("auto_renew") == 1
        self._cap = None

    def compose(self) -> ComposeResult:
        r = self.rig
        with Vertical(id="logview"):
            yield Static(
                f"auto-renew order {r.get('order_id')} -- server "
                f"{r.get('server_id')}  currently "
                f"{'ON' if self.on_now else 'OFF'}", classes="dtitle")
            if self.on_now:
                yield Static(
                    "Disable to let the order expire at its current "
                    "term end. One set_order_auto_renew POST is sent "
                    "-- never retried.")
            else:
                price = r.get("price_day_native")
                yield Static(
                    "Enable to keep billing "
                    f"~${price if price is not None else 'unknown'}"
                    "/day past each term until you cancel. The cap "
                    "below defaults to 1.25x current price (web-UI "
                    "default); renewal is checked each term and can "
                    "still be declined -- host repricing above the "
                    "cap, host disabling renewals, or low balance "
                    "-- and then the order ends. One "
                    "set_order_auto_renew POST, never retried.")
                yield Input(
                    value=(f"{price * 1.25:.4f}" if price is not None
                           else ""),
                    placeholder="max $/day cap", id="rn-cap")
            yield Static("", id="rn-status")
            yield Horizontal(
                Button("Disable renew" if self.on_now
                       else "Enable renew", id="rn-go",
                       variant="default" if self.on_now
                       else "warning"),
                Button("Back", id="rn-back", variant="primary"))

    def on_mount(self):
        self._app = self.app
        if not self.on_now:
            self.query_one("#rn-cap", Input).focus()

    def on_button_pressed(self, event):
        if event.button.id == "rn-back":
            self.dismiss()
            return
        if not self.on_now:
            try:
                cap = float(
                    self.query_one("#rn-cap", Input).value.strip())
            except ValueError:
                cap = None
            if cap is None or not math.isfinite(cap) or cap <= 0:
                self.query_one("#rn-status", Static).update(Text(
                    "cap must be a positive number -- the max $/day "
                    "the renewal may re-bill at"))
                return
            self._cap = cap
        go = self.query_one("#rn-go", Button)
        go.disabled = True
        go.label = "sending..."
        self._append("sending auto-renew request -- waiting for API "
                     "(one POST, no retry)...")
        self._run()

    def _append(self, msg):
        try:
            self.query_one("#rn-status", Static).update(Text(msg))
        except Exception:
            pass  # modal already dismissed

    @work(thread=True, exclusive=True)
    def _run(self):
        import rent

        def collect(msg):
            self._app.call_from_thread(self._append, str(msg))

        try:
            rc, info = rent.set_renewal(
                self.rig["order_id"], self.db_path, DEFAULT_ENV,
                enable=not self.on_now, max_price_day=self._cap,
                say=collect, warn=collect)
        except Exception as exc:
            # last-resort guard -- type name only, never raw text
            rc, info = 1, {"order_id": self.rig.get("order_id"),
                           "stage": type(exc).__name__}
        self._app.call_from_thread(self._finish, rc, info)

    def _finish(self, rc, info):
        try:
            self.query_one("#rn-status", Static).update(Text(
                f"done rc={rc} state={(info or {}).get('state') or '-'}"
                f" stage={(info or {}).get('stage') or '-'}"))
            self.query_one("#rn-back", Button).label = "Close"
        except Exception:
            pass
        order_id = self.rig.get("order_id")
        if rc == 0:
            self._app.notify(f"order {order_id} auto-renew "
                             f"{'OFF' if self.on_now else 'ON'}")
        else:
            self._app.notify(
                f"auto-renew order {order_id} did not complete -- "
                "check the web UI", severity="warning")
        self._app.reload_data()

    def action_close(self):
        self.dismiss()


class RentalSetup(ModalScreen):
    """Collect order terms, then hand a ctx-lite dict back -- the app
    runs rent.quote_order for a live review; nothing spends here."""

    BINDINGS = [Binding("escape", "close", "Cancel")]

    def __init__(self, row):
        super().__init__()
        self.row = row

    def _mrl(self):
        """Host maximum rental hours from the raw listing, else None."""
        try:
            raw = (self.row.get("listing") or {}).get("mrl")
            if isinstance(raw, bool):
                return None
            value = int(raw)
        except (TypeError, ValueError):
            return None
        return value if value >= 1 else None

    def compose(self) -> ComposeResult:
        r = self.row
        mrl = self._mrl()
        hours_hint = (f"Hours (1-{rental_policy.MAX_HOURS}; "
                      f"host max {mrl}h)" if mrl is not None else
                      f"Hours (1-{rental_policy.MAX_HOURS}; "
                      "host max unknown)")
        with Vertical(id="rental"):
            yield Static(
                f"rent server {r.get('server_id')}  -- live quote "
                "review next; nothing spends until Confirm & Rent",
                classes="dtitle")
            yield VerticalScroll(
                Static(Text(
                    f"GPU      {gpu_label(r)}\n"
                    f"CPU      {r.get('cpu') or '?'}\n"
                    f"OD$/day  {fmt_price(r)} (cached, fee-incl.)\n"
                    f"country  {cc_name(r.get('country')) or '--'}\n\n"
                    "On-demand only. Price/fees are re-fetched live "
                    "for the review; the same guarded rules as "
                    "rent.py apply (bounded term, one POST, "
                    "auto-cancel on a bad receipt).\n\n"
                    "Keys: Tab/Shift-Tab moves between fields, "
                    "Space toggles, Enter presses a focused button "
                    "(taps may not reach fields over SSH).")),
                Static("", id="r-error"),
                Static(hours_hint),
                Horizontal(
                    Input(value="1", placeholder="hours 1-24",
                          id="r-hours", classes="rfield"),
                    Button("Max", id="r-max", disabled=mrl is None)),
                Checkbox(
                    "auto-renew (keeps billing each term until you "
                    "cancel)",
                    value=False, id="r-renew"),
                Static("Cost ceiling USD (optional; blank = confirm "
                       "exact quote)"),
                Input(value="", placeholder="blank = exact quote",
                      id="r-cost", classes="rfield"),
                Static("Pool"),
                Select([("jetski", "jetski"), ("qli", "qli")],
                       value="jetski", id="r-pool", classes="rfield"),
                Static("CPU threads (optional, blank = listing)"),
                Input(placeholder="cpu threads (blank = listing)",
                      id="r-threads", classes="rfield"),
                Horizontal(
                    Button("Review & Rent", id="r-go",
                           variant="primary"),
                    Button("Cancel", id="r-cancel")))

    def on_mount(self):
        self.query_one("#r-hours", Input).focus()

    def _fail(self, msg):
        self.query_one("#r-error", Static).update(Text(msg))

    def _submit(self):
        try:
            hours = int(self.query_one("#r-hours", Input).value.strip())
        except ValueError:
            self._fail("hours must be an integer 1.."
                       f"{rental_policy.MAX_HOURS}")
            return
        if not 1 <= hours <= rental_policy.MAX_HOURS:
            self._fail("hours must be 1.."
                       f"{rental_policy.MAX_HOURS}")
            return
        cost_text = self.query_one("#r-cost", Input).value.strip()
        cap = None
        if cost_text:
            try:
                cap = rental_policy.amount(cost_text,
                                           "maximum order cost")
            except ValueError:
                self._fail("cost ceiling must be a finite positive "
                           "USD amount or blank")
                return
        threads_text = self.query_one("#r-threads", Input).value.strip()
        threads = None
        if threads_text:
            try:
                threads = int(threads_text)
            except ValueError:
                self._fail("cpu threads must be an integer 1..512 "
                           "or blank")
                return
            if not 1 <= threads <= 512:
                self._fail("cpu threads must be 1..512 or blank")
                return
        pool = self.query_one("#r-pool", Select).value
        self.dismiss({"server_id": self.row.get("server_id"),
                      "hours": hours,
                      "auto_renew": bool(
                          self.query_one("#r-renew", Checkbox).value),
                      "max_cost_usd": None if cap is None
                      else str(cap),
                      "pool": str(pool), "cpu_threads": threads})

    def on_button_pressed(self, event):
        if event.button.id == "r-go":
            self._submit()
        elif event.button.id == "r-max":
            mrl = self._mrl()
            if mrl is not None:
                self.query_one("#r-hours", Input).value = str(mrl)
        else:
            self.dismiss(None)

    def action_close(self):
        self.dismiss(None)


class StrategyModal(ModalScreen):
    """Edit the open phase's score yardstick and the gates the named
    strategy bundles move. Blank input = leave unchanged; Apply runs
    one validated set_phase_config (the runner re-reads it live)."""

    BINDINGS = [Binding("escape", "close", "Cancel")]

    # (config key, label); all floats except max_price_hr (Decimal)
    LIVE_FIELDS = [
        ("upgrade_margin", "upgrade margin (beat incumbent by 1+x)"),
        ("promote_margin", "promote margin (probe beats slot by 1+x)"),
        ("max_price_hr", "max price $/hr"),
    ]
    # inert while loss_mining is checked -- rendered greyed out
    GATE_FIELDS = [
        ("min_value_ratio", "min value ratio (floor x best prior)"),
        ("min_return", "min return (predicted $ payout per $ cost)"),
    ]
    FIELDS = LIVE_FIELDS + GATE_FIELDS
    # integer fleet-sizing bounds; slot_min only matters in dynamic
    # slot_mode and is greyed out under static
    INT_FIELDS = [
        ("slot_min", "min slots (hunting floor, dynamic mode only)"),
        ("max_rentals", "max slots (ceiling the scaler can grow to)"),
    ]
    # dynamic-only growth/shrink policy: a rig must beat its peers'
    # mean score by 1+growth_margin to count exceptional (earn a
    # growth seat); shrink_mode picks how an over-target fleet drains
    FLEET_FLOAT_FIELDS = [
        ("growth_margin",
         "growth margin (0.25 = exceptional needs 1.25x peer mean)"),
    ]
    SHRINK_MODES = ("passive", "no_renew", "cull")
    # dry-timing knobs: startup_min is a fresh rig's grace to land its
    # first share; dry_after_min is the silence that marks a
    # never-producer for cancel and a quiet producer as not-producing
    TIMING_FIELDS = [
        ("startup_min", "startup grace min (fresh rig's time to first share)"),
        ("dry_after_min", "dry after min (silence before cancel/replace)"),
    ]
    # spot posture per role: auto = spot only when the discount clears
    # and a moved market kills the pick; prefer = same, but falls back
    # to on-demand on a recheck abort; only = no spot quote, no rent;
    # off = never quote spot
    SPOT_MODES = ("auto", "prefer", "only", "off")
    SPOT_SELECTS = [
        ("spot_probe_mode", "spot probes"),
        ("spot_slot_mode", "spot slots"),
    ]
    SPOT_FLOAT_FIELDS = [
        ("spot_bid_premium",
         "spot bid premium (1.01 = min legal margin over top bid)"),
    ]

    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg

    def compose(self) -> ComposeResult:
        with Vertical(id="rental"):
            yield Static(
                "experiment strategy -- the score candidates and "
                "incumbents are ranked on plus the gates around it; "
                "blank fields stay unchanged", classes="dtitle")
            yield VerticalScroll(
                Static("score"),
                Select([(k, k) for k in ("net", "gross", "its")],
                       value=self.cfg.get("score") or "net",
                       id="st-score", classes="rfield"),
                Static(self._score_help(self.cfg.get("score")),
                       id="st-score-help"),
                *[item for k, lbl in self.LIVE_FIELDS
                    for item in (
                        Static(lbl),
                        Input(value=str(self.cfg.get(k) or ""),
                              placeholder=k, id=f"st-{k}",
                              classes="rfield"))],
                Checkbox(
                    "loss_mining (ignore floor/return gates; "
                    "violating picks flagged)",
                    value=bool(self.cfg.get("loss_mining")),
                    id="st-loss"),
                Vertical(
                    *[item for k, lbl in self.GATE_FIELDS
                        for item in (
                            Static(lbl),
                            Input(value=str(self.cfg.get(k) or ""),
                                  placeholder=k, id=f"st-{k}",
                                  classes="rfield"))],
                    id="st-gates"),
                Checkbox(
                    "paused (no new rents/bursts; cancels and "
                    "judgements keep running)",
                    value=bool(self.cfg.get("paused")),
                    id="st-paused"),
                Checkbox(
                    "probes_enabled (burst auditions; off = slots "
                    "cook, vacancies fill direct, no probe spend)",
                    value=bool(self.cfg.get("probes_enabled", True)),
                    id="st-probes"),
                Static("fleet"),
                Select([(k, k) for k in ("dynamic", "static")],
                       value=self.cfg.get("slot_mode") or "static",
                       id="st-slot_mode", classes="rfield"),
                Static("slot_mode -- dynamic grows to exceptional "
                       "slots +1 within [slot_min, max_rentals]; "
                       "static always fills max_rentals",
                       id="st-slots-help"),
                Vertical(
                    *[item for k, lbl in self.INT_FIELDS
                        for item in (
                            Static(lbl),
                            Input(value=str(self.cfg.get(k) or ""),
                                  placeholder=k, id=f"st-{k}",
                                  classes="rfield"))],
                    id="st-ints"),
                Vertical(
                    *[item for k, lbl in self.FLEET_FLOAT_FIELDS
                        for item in (
                            Static(lbl),
                            Input(value=str(self.cfg.get(k) or ""),
                                  placeholder=k, id=f"st-{k}",
                                  classes="rfield"))],
                    Select([(k, k) for k in self.SHRINK_MODES],
                           value=self.cfg.get("shrink_mode")
                           or "passive",
                           id="st-shrink_mode", classes="rfield"),
                    Static("shrink_mode -- over-target policy: "
                           "passive = attrition only; no_renew = "
                           "weakest excess exits at term end; "
                           "cull = cancel weakest below-average "
                           "excess each cycle",
                           id="st-shrink-help"),
                    id="st-fleet-dyn"),
                Static("timing"),
                Vertical(
                    *[item for k, lbl in self.TIMING_FIELDS
                        for item in (
                            Static(lbl),
                            Input(value=str(self.cfg.get(k) or ""),
                                  placeholder=k, id=f"st-{k}",
                                  classes="rfield"))],
                    id="st-timing"),
                Static("spot"),
                Checkbox(
                    "spot_enabled (quote spot where it clears the "
                    "discount)",
                    value=bool(self.cfg.get("spot_enabled")),
                    id="st-spot"),
                *[item for k, lbl in self.SPOT_SELECTS
                    for item in (
                        Static(lbl),
                        Select([(k, k) for k in self.SPOT_MODES],
                               value=self.cfg.get(k) or "auto",
                               id=f"st-{k}", classes="rfield"))],
                *[item for k, lbl in self.SPOT_FLOAT_FIELDS
                    for item in (
                        Static(lbl),
                        Input(value=str(self.cfg.get(k) or ""),
                              placeholder=k, id=f"st-{k}",
                              classes="rfield"))],
                Static("", id="st-error"),
                Horizontal(
                    Button("Apply", id="st-go", variant="primary"),
                    Button("Cancel", id="st-cancel")))

    @staticmethod
    def _score_help(value):
        from experiment import SCORE_HELP
        return SCORE_HELP.get(value or "net", "")

    def on_select_changed(self, event):
        if event.select.id == "st-score":
            self.query_one("#st-score-help", Static).update(
                self._score_help(str(event.value)))
        elif event.select.id == "st-slot_mode":
            self._sync_slots()

    def on_checkbox_changed(self, event):
        if event.checkbox.id == "st-loss":
            self._sync_gates()
        elif event.checkbox.id == "st-spot":
            self._sync_spot()

    def _sync_gates(self):
        off = bool(self.query_one("#st-loss", Checkbox).value)
        for key, _ in self.GATE_FIELDS:
            self.query_one(f"#st-{key}", Input).disabled = off
        self.query_one("#st-gates").set_class(off, "gate-off")

    def _sync_slots(self):
        static = str(self.query_one(
            "#st-slot_mode", Select).value) == "static"
        self.query_one("#st-slot_min", Input).disabled = static
        for key, _ in self.FLEET_FLOAT_FIELDS:
            self.query_one(f"#st-{key}", Input).disabled = static
        self.query_one("#st-shrink_mode", Select).disabled = static
        self.query_one("#st-fleet-dyn").set_class(static, "gate-off")

    def _sync_spot(self):
        off = not bool(self.query_one("#st-spot", Checkbox).value)
        for key, _ in self.SPOT_SELECTS:
            self.query_one(f"#st-{key}", Select).disabled = off
        for key, _ in self.SPOT_FLOAT_FIELDS:
            self.query_one(f"#st-{key}", Input).disabled = off

    def on_mount(self):
        self._sync_gates()
        self._sync_slots()
        self._sync_spot()
        self.query_one("#st-score", Select).focus()

    def _submit(self):
        updates = {}
        for key, _ in self.FIELDS:
            inp = self.query_one(f"#st-{key}", Input)
            if inp.disabled:
                continue
            raw = inp.value.strip()
            if not raw:
                continue
            if key == "max_price_hr":
                updates[key] = raw
                continue
            try:
                updates[key] = float(raw)
            except ValueError:
                self.query_one("#st-error", Static).update(
                    Text(f"{key} must be a number (or blank)"))
                return
        score = str(self.query_one("#st-score", Select).value)
        if score != (self.cfg.get("score") or "net"):
            updates["score"] = score
        loss = bool(self.query_one("#st-loss", Checkbox).value)
        if loss != bool(self.cfg.get("loss_mining")):
            updates["loss_mining"] = loss
        paused = bool(self.query_one("#st-paused", Checkbox).value)
        if paused != bool(self.cfg.get("paused")):
            updates["paused"] = paused
        probes = bool(self.query_one("#st-probes", Checkbox).value)
        if probes != bool(self.cfg.get("probes_enabled", True)):
            updates["probes_enabled"] = probes
        spot_on = bool(self.query_one("#st-spot", Checkbox).value)
        if spot_on != bool(self.cfg.get("spot_enabled")):
            updates["spot_enabled"] = spot_on
        slot_mode = str(self.query_one("#st-slot_mode", Select).value)
        if slot_mode != (self.cfg.get("slot_mode") or "static"):
            updates["slot_mode"] = slot_mode
        if slot_mode != "static":
            for key, _ in self.FLEET_FLOAT_FIELDS:
                inp = self.query_one(f"#st-{key}", Input)
                raw = inp.value.strip()
                if not raw:
                    continue
                try:
                    updates[key] = float(raw)
                except ValueError:
                    self.query_one("#st-error", Static).update(
                        Text(f"{key} must be a number (or blank)"))
                    return
            shrink = str(self.query_one(
                "#st-shrink_mode", Select).value)
            if shrink != (self.cfg.get("shrink_mode") or "passive"):
                updates["shrink_mode"] = shrink
        for key, _ in (self.INT_FIELDS + self.TIMING_FIELDS):
            inp = self.query_one(f"#st-{key}", Input)
            if inp.disabled:
                continue
            raw = inp.value.strip()
            if not raw:
                continue
            try:
                updates[key] = int(raw)
            except ValueError:
                self.query_one("#st-error", Static).update(
                    Text(f"{key} must be a whole number (or blank)"))
                return
        if not spot_on:
            for key, _ in (self.SPOT_SELECTS + self.SPOT_FLOAT_FIELDS):
                updates.pop(key, None)
        else:
            for key, _ in self.SPOT_SELECTS:
                sel = str(self.query_one(f"#st-{key}", Select).value)
                if sel != (self.cfg.get(key) or "auto"):
                    updates[key] = sel
            for key, _ in self.SPOT_FLOAT_FIELDS:
                inp = self.query_one(f"#st-{key}", Input)
                raw = inp.value.strip()
                if not raw:
                    continue
                try:
                    updates[key] = float(raw)
                except ValueError:
                    self.query_one("#st-error", Static).update(
                        Text(f"{key} must be a number (or blank)"))
                    return
        self.dismiss(updates)

    def on_button_pressed(self, event):
        if event.button.id == "st-go":
            self._submit()
        else:
            self.dismiss(None)

    def action_close(self):
        self.dismiss(None)


class ReviewModal(ModalScreen):
    """Scrollable order review: the exact bounded quote the guarded
    flow computed. Confirm & Rent starts create_order_flow; Cancel (or
    Escape) places nothing."""

    BINDINGS = [Binding("escape", "close", "Cancel")]

    def __init__(self, row, ctx, on_confirm=None, on_cancel=None):
        super().__init__()
        self.row = row
        self.ctx = ctx
        self.on_confirm = on_confirm
        self.on_cancel = on_cancel

    def compose(self) -> ComposeResult:
        r, ctx = self.row, self.ctx
        q = ctx["quote"]
        server = ctx["server"]
        specs = server.get("specs") or {}
        cap = ctx.get("max_cost_usd")
        ceiling = ("none -- you confirm the exact bound"
                   if cap in (None, "") else f"${cap}")
        lines = [
            f"server       {ctx['server_id']}",
            f"gpu          {gpu_label(r)}",
            f"cpu model    {r.get('cpu') or specs.get('cpu') or '?'}",
            f"threads      listed "
            f"{q.get('listed_cpu_allocation') or '?'} -> chosen "
            f"{q.get('cpu_threads')}",
            f"pool         {ctx['pool']}   alias {ctx['alias']}",
            (f"hours        {q.get('hours')}  per term, "
             "auto-renew ON" if q.get("auto_renew") is True else
             f"hours        {q.get('hours')}  auto-expires, "
             "renewal off"),
            f"cost bound   ${q.get('cost_bound_usd')} incl live fees "
            f"(hourly ${q.get('hourly_cost_usd')}, creation "
            f"${q.get('creation_fee_usd')})",
            f"ceiling      {ceiling}",
            f"ssh key      {ctx['key_path']}",
            "",
        ]
        if q.get("auto_renew") is True:
            lines.append(
                f"auto-renew ON -- re-bills ~${q.get('cost_bound_usd')} "
                f"every {q.get('hours')}h term until cancelled "
                "(bound covers ONE term only)")
        lines += [
            "terms: auto-cancel if the receipt lacks the bounded "
            "term,",
            "or if provisioning fails.",
            "risk: the host can technically read rig memory; "
            "credentials",
            "are pushed to the rig.",
        ]
        with Vertical(id="review"):
            yield Static("REVIEW ORDER -- nothing placed yet",
                         classes="dtitle")
            if ctx.get("blacklist"):
                import rent
                yield Static(Text(
                    "WARNING: " + rent.blacklist_warning(ctx["blacklist"]),
                    style="bold yellow"))
            yield VerticalScroll(Static(Text("\n".join(lines))))
            yield Horizontal(
                Button("Confirm & Rent", id="r-confirm",
                       variant="primary"),
                Button("Cancel", id="r-cancel"))

    def _confirm(self):
        cb = self.on_confirm
        self.dismiss()
        if cb is not None:
            cb(self.ctx)

    def _cancel(self):
        cb = self.on_cancel
        self.dismiss()
        if cb is not None:
            cb()

    def on_button_pressed(self, event):
        if event.button.id == "r-confirm":
            self._confirm()
        else:
            self._cancel()

    def action_close(self):
        self._cancel()


# --- log colorization --------------------------------------------------
_LOG_ANSI_RE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[()#][0-9A-Za-z]")
_LOG_TOKEN_RE = re.compile(
    r"\[(?P<lvl>INFO|WARN(?:ING)?|ERROR|FATAL|CRIT(?:ICAL)?|DEBUG|TRACE)\]"
    r"|(?P<dev>\[(?:CPU|GPU|CUDA|AVX2|AVX512|SSE\d*)\]"
    r"|\b(?:QUBJETSKI|QLI|AVX2|AVX512|CUDA)\b|\bGPU\s*#\d+\b)"
    r"|(?P<ts>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?"
    r"(?:Z|[+-]\d{2}:?\d{2})?|\b\d{2}:\d{2}:\d{2}\b)"
    r"|SHARES:\s*(?P<sf>\d+)/(?P<ss>\d+)\s*\(R:(?P<sr>\d+)\)"
    r"|(?P<epoch>\bE:\d+)"
    r"|(?P<rate>\b\d+\s+(?:avg\s+)?it/s\b)"
    r"|(?P<key>\b(?:ID|SEED):)"
    r"|(?P<ref>#\d+|\bs\d+\b)"
    r"|(?P<kv>\b[a-zA-Z_][a-zA-Z0-9_]*=)"
    r"|(?P<info>\bburst\b|\brerent\b|\bjudge[sd]?\b)"
    r"|(?P<kbad>\bblocked\b|\bblacklist(?:ed)?\b)"
    r"|(?P<kwarn>\bcancell?ed\b|\bended\b|\brestart(?:ed|s)?\b|"
    r"\bexpired\b)"
    r"|(?P<bad>\b(?:error|fail(?:ed|ure|s)?|fatal|refused|denied|"
    r"timeout|timed out|reject(?:ed|ion|s)?|stall(?:ed|s)?|invalid|"
    r"unreachable|ambiguous|unable|abort(?:ed|ing)?|stale|lost|"
    r"disconnect(?:ed|s)?)\b)"
    r"|(?P<good>\bfirst\s+share\b|\bshare\s*found\b|\brent\b|"
    r"\balive\b|\baccepted\b|\bready\b|\bconnected\b)",
    re.IGNORECASE)

_LOG_DIM = "#6b7280"
_LOG_TEAL = "#2dd4bf"
_LOG_WARN = "#f59e0b"
_LOG_ERR = "#f87171"
_LOG_GOOD = "#4ade80"
_LOG_EPOCH = "#bb9af7"
_LOG_LVL = {"INFO": _LOG_DIM, "WARN": _LOG_WARN, "WARNING": _LOG_WARN,
            "ERROR": "bold " + _LOG_ERR, "FATAL": "bold " + _LOG_ERR,
            "CRIT": "bold " + _LOG_ERR, "CRITICAL": "bold " + _LOG_ERR,
            "DEBUG": _LOG_DIM, "TRACE": _LOG_DIM}
_ERR_LVLS = {"ERROR", "FATAL", "CRIT", "CRITICAL"}
_WARN_LVLS = {"WARN", "WARNING"}


def _colorize_log_line(line):
    """One log line -> Text. The whole line is tinted red when it
    carries an error tag/word and amber for [WARN]; timestamps are
    dimmed and known tokens recolored on top."""
    matches = list(_LOG_TOKEN_RE.finditer(line))
    base = None
    for m in matches:
        lvl = m.group("lvl")
        if (lvl and lvl.upper() in _ERR_LVLS) or m.group("bad") \
                or m.group("kbad"):
            base = _LOG_ERR
            break
        if (lvl and lvl.upper() in _WARN_LVLS) or m.group("kwarn"):
            base = _LOG_WARN
    out = Text()
    pos = 0
    for m in matches:
        if m.start() > pos:
            out.append(line[pos:m.start()], style=base)
        if m.group("lvl"):
            out.append(m.group(0), style=_LOG_LVL[m.group("lvl").upper()])
        elif m.group("dev"):
            out.append(m.group(0), style=_LOG_TEAL)
        elif m.group("ts"):
            out.append(m.group(0), style=_LOG_DIM)
        elif m.group("sf") is not None:
            out.append(line[m.start():m.start("sf")], style=_LOG_TEAL)
            out.append(line[m.start("sf"):m.end("ss")],
                       style=_LOG_GOOD if int(m.group("sf")) else _LOG_DIM)
            out.append(line[m.end("ss"):m.end()],
                       style=_LOG_ERR if int(m.group("sr")) else _LOG_DIM)
        elif m.group("epoch"):
            out.append(m.group(0), style=_LOG_EPOCH)
        elif m.group("rate"):
            out.append(m.group(0), style=_LOG_GOOD)
        elif m.group("key"):
            out.append(m.group(0), style=_LOG_WARN)
        elif m.group("ref"):
            out.append(m.group(0), style=_LOG_EPOCH)
        elif m.group("kv"):
            out.append(m.group(0), style=_LOG_DIM)
        elif m.group("info"):
            out.append(m.group(0), style=_LOG_TEAL)
        elif m.group("kbad"):
            out.append(m.group(0), style=_LOG_ERR)
        elif m.group("kwarn"):
            out.append(m.group(0), style=_LOG_WARN)
        elif m.group("bad"):
            out.append(m.group(0), style=_LOG_ERR)
        else:
            out.append(m.group(0), style=_LOG_GOOD)
        pos = m.end()
    if pos < len(line):
        out.append(line[pos:], style=base)
    return out


def colorize_log(text):
    """Miner/rent log text -> Text with semantic colors. ANSI escapes
    are stripped first (raw codes would render as garbage)."""
    out = Text()
    for i, raw in enumerate(text.split("\n")):
        if i:
            out.append("\n")
        out.append_text(
            _colorize_log_line(_LOG_ANSI_RE.sub("", raw)))
    return out


class ProgressModal(ModalScreen):
    """Live timestamped log of the guarded create flow. Escape/Hide
    only hides the view -- the worker keeps running (there is no
    cancel path for a paid order from here); `b` reopens it."""

    BINDINGS = [Binding("escape", "close", "Hide")]

    def __init__(self, title="rent in progress -- guarded flow log"):
        super().__init__()
        self._lines = []
        self.done = False
        self._title = title

    def compose(self) -> ComposeResult:
        with Vertical(id="progress"):
            yield Static(self._title, classes="dtitle")
            yield VerticalScroll(Static("", id="plog"))
            yield Static("running... -- Hide keeps it going; "
                         "press b to reopen", id="pstatus")
            yield Horizontal(Button("Hide", id="p-close"))

    def on_mount(self):
        self._paint_log()

    def add_line(self, msg):
        self._lines.append(msg)
        self._paint_log()

    def _paint_log(self):
        try:
            self.query_one("#plog", Static).update(
                colorize_log("\n".join(self._lines[-400:])))
        except Exception:
            pass

    def finish(self, status):
        self.done = True
        try:
            self.query_one("#pstatus", Static).update(Text(status))
            self.query_one("#p-close", Button).label = "Close"
        except Exception:
            pass

    def on_button_pressed(self, event):
        if event.button.id == "p-close":
            self.dismiss()

    def action_close(self):
        self.dismiss()


class LogModal(ModalScreen):
    """One explicit read-only SSH fetch of a rig's miner log tail.
    Refresh refetches; Escape/Close dismisses. No polling."""

    BINDINGS = [Binding("escape", "close", "Close")]

    def __init__(self, rig, pool, lines=200, db_path=None):
        super().__init__()
        self.rig = rig
        self.pool = pool
        self.lines = lines
        self.db_path = db_path

    def compose(self) -> ComposeResult:
        r = self.rig
        with Vertical(id="logview"):
            yield Static(
                f"miner log -- order {r.get('order_id')} "
                f"pool {self.pool} (tail {self.lines})",
                classes="dtitle")
            yield Static("fetching...", id="lstatus")
            yield VerticalScroll(Static("", id="llog"))
            yield Horizontal(
                Button("Refresh", id="l-refresh"),
                Button("Close", id="l-close", variant="primary"))

    def on_mount(self):
        self._app = self.app
        self._fetch()

    @work(thread=True, exclusive=True)
    def _fetch(self):
        import rig_logs
        import rig_ssh
        r = self.rig
        try:
            text = rig_logs.tail_miner_log(
                r["ssh_host"], int(r["ssh_port"]),
                r["ssh_key_path"], r["order_id"], self.pool,
                lines=self.lines)
            err = None
        except (rig_ssh.SSHError, ValueError, OSError) as exc:
            text, err = "", str(exc)
        except Exception as exc:
            # never surface raw exception text that could carry secrets
            text, err = "", type(exc).__name__
        archive = None
        if err is not None and self.db_path:
            archive = read_log_archive(
                self.db_path, r["order_id"], self.pool)
            if not archive.get("text"):
                archive = None
        self._app.call_from_thread(self._show, text, err, archive)

    def _show(self, text, err, archive=None):
        try:
            status = self.query_one("#lstatus", Static)
            log = self.query_one("#llog", Static)
        except Exception:
            return  # modal already dismissed
        if err is not None:
            if archive:
                status.update(Text(
                    f"live fetch failed: {err} -- archived copy, "
                    f"last captured {archive['last_captured_at']}",
                    style=_LOG_WARN))
                log.update(colorize_log("\n".join(
                    archive["text"].splitlines()[-self.lines:])))
            else:
                status.update(Text(f"fetch failed: {err}",
                                   style=_LOG_ERR))
                log.update(Text("(no log)"))
            return
        stamp = datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        status.update(Text(
            f"fetched {stamp} UTC -- {len(text)} chars"))
        log.update(
            colorize_log(text) if text else Text("(empty log)"))

    def on_button_pressed(self, event):
        if event.button.id == "l-refresh":
            self.query_one("#lstatus", Static).update(
                Text("fetching..."))
            self._fetch()
        else:
            self.dismiss()

    def action_close(self):
        self.dismiss()


class LogTextArea(TextArea):
    """Read-only log pane with working copy: the stock ctrl+c only
    emits an OSC 52 escape, which tmux and many terminals swallow;
    MarketTui.copy_to_clipboard additionally calls the local clipboard
    tools. Right-click copies the selection (or the clicked line when
    nothing is selected); a right-press must not reset the selection
    the way a left-press does."""

    def get_line(self, line_index):
        # documented styling hook: the selection/cursor styles are
        # applied on top of what we return, so colorize per line
        line = _colorize_log_line(_LOG_ANSI_RE.sub(
            "", self.document.get_line(line_index)))
        line.end = ""
        line.no_wrap = True
        return line

    def action_copy(self):
        text = self.selected_text
        if not text:
            self.app.notify("nothing selected", severity="warning")
            return
        super().action_copy()
        self.app.notify_copied(f"copied {len(text)} chars")

    async def _on_mouse_down(self, event):
        if event.button != 1:
            event.stop()
            return
        await super()._on_mouse_down(event)

    def _on_click(self, event):
        if event.button != 3:
            return
        event.stop()
        text = self.selected_text
        if not text:
            row, _ = self.get_target_document_location(event)
            if row < self.document.line_count:
                line = self.document[row]
                if line:
                    self.selection = Selection(
                        (row, 0), (row, len(line)))
                    text = line
        if text:
            self.app.copy_to_clipboard(text)
            self.app.notify_copied(f"copied {len(text)} chars")
        else:
            self.app.notify("nothing to copy", severity="warning")


class MarketTui(App):
    TITLE = "clorqub marketplace"
    CSS = CSS
    # drags scroll (touch-style); copying is handled by LogTextArea
    # (ctrl+c / right-click), not the terminal's own selection
    ALLOW_SELECT = False
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "reload", "Reload db"),
        Binding("a", "toggle_available", "Avail only"),
        Binding("s", "toggle_sort", "Sort $/day"),
        Binding("f", "toggle_fleet_all", "Fleet all"),
        Binding("/", "focus_search", "Search"),
        Binding("c", "reset_filters", "Reset"),
        Binding("b", "rent_selected",
                "Rent"),
        Binding("l", "log_selected", "Miner log"),
        Binding("e", "toggle_loss_mining", "LossMining"),
        Binding("g", "strategy", "Strategy"),
        Binding("?", "help", "Help"),
        Binding("escape", "leave_search", "Back", show=False),
    ]

    def __init__(self, db_path, refresh):
        super().__init__()
        self.db_path = db_path
        self.refresh_seconds = max(1, refresh)
        self.dash = None
        self.error = None
        self.avail_only = False
        self.sort_by_price = False
        self.fleet_show_all = False
        self.fingerprint = None
        self.reloaded_at = None
        self.sel_id = {"cand": None, "mkt": None}
        self.sort_cols = {}  # table id -> (ColumnKey, reverse)
        self._rent_active = False
        self._rent_log = []
        self._rent_modal = None
        self._search_pinned = True
        self._compact = False
        self._drag_scroll = None
        self._clipboard_tool = None
        # gentler horizontal pointer/trackpad scrolling (default is 4.0)
        self.scroll_sensitivity_x = 2.0

    def copy_to_clipboard(self, text):
        """OSC 52 (works over SSH) plus local tools -- the escape alone
        is dropped by tmux and many terminals. Records the tool used in
        `self._clipboard_tool` for notify_copied() feedback."""
        super().copy_to_clipboard(text)
        self._clipboard_tool = clipboard.copy_text(text)

    def notify_copied(self, label):
        """Toast feedback after copy_to_clipboard."""
        if self._clipboard_tool:
            self.notify(f"{label} ({self._clipboard_tool})")
        else:
            self.notify(
                f"{label}: sent via terminal escape; if paste fails "
                "install xclip/wl-copy (or check tmux set-clipboard)",
                severity="warning")

    def compose(self) -> ComposeResult:
        with Vertical(id="hdr"):
            yield Static(Text.assemble(
                ("cq marketplace", "#2dd4bf"),
                ("   OPERATOR", "#f59e0b")), id="h1")
            yield Static("", id="h2")
            yield Static("", id="h3")
        yield Static("", id="banner")
        with Horizontal(id="search"):
            yield Input(placeholder="/ filter: id owner cc cpu gpu isa reason")
        with TabbedContent():
            with TabPane("Candidates", id="candidates"):
                with Horizontal(classes="twrap"):
                    yield Button("◀", id="cand-left", classes="nudge")
                    yield DataTable(id="cand", cursor_type="row")
                    yield Button("▶", id="cand-right", classes="nudge")
            with TabPane("Marketplace", id="marketplace"):
                with Horizontal(classes="twrap"):
                    yield Button("◀", id="mkt-left", classes="nudge")
                    yield DataTable(id="mkt", cursor_type="row")
                    yield Button("▶", id="mkt-right", classes="nudge")
            with TabPane("Fleet", id="fleet"):
                with Vertical():
                    yield Static("", id="fleet-status")
                    with Horizontal(classes="twrap"):
                        yield Button("◀", id="fleet-table-left",
                                     classes="nudge")
                        yield DataTable(id="fleet-table",
                                        cursor_type="row")
                        yield Button("▶", id="fleet-table-right",
                                     classes="nudge")
            with TabPane("Experiment", id="experiment"):
                with Vertical():
                    yield Static("", id="exp-status")
                    yield Static("", id="exp-upgrade")
                    with Horizontal(classes="twrap"):
                        yield Button("◀", id="exp-table-left",
                                     classes="nudge")
                        yield DataTable(id="exp-table",
                                        cursor_type="row")
                        yield Button("▶", id="exp-table-right",
                                     classes="nudge")
                    yield LogTextArea(id="exp-log", read_only=True)
        yield Footer()

    def on_mount(self):
        self.query_one("#cand", DataTable).add_columns(*CAND_COLS)
        self.query_one("#mkt", DataTable).add_columns(*MKT_COLS)
        self.query_one("#exp-table", DataTable).add_columns(*EXP_COLS)
        self.query_one("#fleet-table", DataTable).add_columns(*FLEET_COLS)
        self.set_interval(self.refresh_seconds, self.action_reload)
        self.action_reload()
        self.call_after_refresh(
            lambda: self.query_one("#cand", DataTable).focus())
        self.call_after_refresh(self._update_compact)

    def on_resize(self, event):
        self._update_compact(event.size)

    def _update_compact(self, size=None):
        """Small viewports (phones, keyboard open): drop the search row
        and stats line so the table and tab status stay visible.
        `/` reveals the search row again; Enter/Escape hides it."""
        size = size or self.screen.size
        compact = size.height < 15 or size.width < 50
        if compact and not self._compact:
            # entering a small viewport: reclaim the filter row once;
            # after that the user controls it via `/` and Escape
            self._search_pinned = False
        self._compact = compact
        self.set_class(compact, "compact")
        try:
            self.query_one("#search").display = self._search_pinned
        except Exception:
            pass

    # -- data --------------------------------------------------------------

    def action_reload(self):
        self.reload_data()

    def action_toggle_loss_mining(self):
        """Flip loss_mining on the open phase -- the runner re-reads
        config_json each cycle, so this takes effect within one tick.
        When on, the below_floor/below_return gates are dropped
        outright: probes, slot fills and burst promotion rank purely
        on score, and picks that would have failed are flagged
        loss_mining. It is the operator's call whether the current
        QU price justifies mining at a loss until an upgrade frees
        a better rig."""
        if len(self.screen_stack) != 1:
            return
        import experiment
        try:
            db = experiment.open_db(self.db_path)
            try:
                ph = db.execute(
                    "SELECT config_json FROM experiment_phases "
                    "WHERE closed_at IS NULL ORDER BY phase_id DESC "
                    "LIMIT 1").fetchone()
                if ph is None:
                    self.notify("no open experiment phase",
                                severity="warning")
                    return
                cur = json.loads(ph["config_json"]).get(
                    "loss_mining", False)
                new = experiment.set_phase_config(
                    db, loss_mining=not cur)["loss_mining"]
            finally:
                db.close()
        except Exception as exc:
            self.notify(f"loss_mining toggle failed: {exc}",
                        severity="error")
            return
        self.notify(f"loss_mining {'on' if new else 'off'}")
        self.action_reload()

    def action_strategy(self):
        """Open the strategy modal: score yardstick + the gates the
        named bundles move, applied live via set_phase_config."""
        if len(self.screen_stack) != 1:
            return
        import experiment
        try:
            db = experiment.open_db(self.db_path)
            try:
                ph = db.execute(
                    "SELECT config_json FROM experiment_phases "
                    "WHERE closed_at IS NULL ORDER BY phase_id DESC "
                    "LIMIT 1").fetchone()
            finally:
                db.close()
            if ph is None:
                self.notify("no open experiment phase",
                            severity="warning")
                return
            cfg = json.loads(ph["config_json"])
        except Exception as exc:
            self.notify(f"strategy open failed: {exc}",
                        severity="error")
            return
        self.push_screen(
            StrategyModal(cfg),
            lambda updates: self._strategy_apply(updates))

    def _strategy_apply(self, updates):
        if not updates:
            return
        import experiment
        try:
            db = experiment.open_db(self.db_path)
            try:
                cfg = experiment.set_phase_config(db, **updates)
            finally:
                db.close()
            if cfg is None:
                self.notify("no open experiment phase",
                            severity="warning")
                return
        except Exception as exc:
            self.notify(f"strategy apply failed: {exc}",
                        severity="error")
            return
        self.notify("strategy: " + " ".join(
            f"{k}={cfg[k]}" for k in updates))
        self.action_reload()

    @work(thread=True, exclusive=True)
    def reload_data(self):
        try:
            dash = read_dashboard(self.db_path)
            error = None
        except DashboardError as exc:
            dash = None
            error = str(exc)
        except Exception as exc:  # reader should only raise DashboardError
            dash = None
            error = f"{type(exc).__name__}"
        self.call_from_thread(self.apply_data, dash, error)

    def apply_data(self, dash, error):
        self.error = error
        self.reloaded_at = datetime.now()
        if dash is not None:
            self.dash = dash
            fp = (dash.get("snapshot_id"), dash["captured_at"],
                  dash["source"], dash["total"], dash["ranked"],
                  dash.get("spot_signature"),
                  dash.get("blacklist_signature"),
                  (dash.get("order_inventory") or {}).get("revision"),
                  dash.get("fleet_signature"),
                  dash.get("experiment_signature"),
                  dash.get("candidate_signature"),
                  self.avail_only, self.sort_by_price,
                  self.fleet_show_all,
                  self._search_text())
            if fp != self.fingerprint:
                self.fingerprint = fp
                self.render_tables()
            self.update_header()
        else:
            self.update_header()
            if self.dash is None:
                self.render_tables()
        banner = self.query_one("#banner", Static)
        if self.error:
            prev = ""
            if self.dash is not None:
                prev = (f" -- showing STALE data from "
                        f"{self.dash['captured_at']} "
                        f"({age_of(self.dash['captured_at'])})")
            banner.update(Text(
                f"RELOAD FAILED: {self.error}{prev}. Fix and press r."))
            banner.add_class("show")
        elif self.dash is not None and not self.dash["total"]:
            banner.update(Text(
                "Latest snapshot contains 0 listings. "
                "Run `python3 cli.py scan --live` to import one."))
            banner.add_class("show")
        elif self.dash is not None:
            banner.remove_class("show")

    def update_header(self):
        h2 = self.query_one("#h2", Static)
        h3 = self.query_one("#h3", Static)
        if self.dash is None:
            h2.update(Text("CACHED SNAPSHOT  no data loaded"))
            h3.update(Text(""))
            return
        d = self.dash
        kind = (d["source"] or "?").split(":", 1)[0]
        bal = d.get("balance_usd")
        phase = (d.get("experiment") or {}).get("phase") or {}
        money = ""
        if phase:
            opt = phase.get("optimal_burn_hr")
            burn = phase.get("active_burn_hr")
            # flag when the actual burn lags the epoch-end pace the
            # way decide() does (>10% deficit)
            low = (opt is not None and burn is not None
                   and float(burn) < 0.9 * float(opt))
            money = (
                f"BAL {'$' + format(bal, '.2f') if bal is not None else '-'}  "
                f"BUD ${phase.get('committed', '?')}"
                f"/${phase.get('budget', '?')}  "
                f"BURN {('$' + format(float(burn), '.3f') + '/h')
                        if burn is not None else '-'}"
                f"{'!' if low else ''}  "
                f"OPT {('$' + format(float(opt), '.3f') + '/h')
                       if opt is not None else '-'}  "
                f"{_ep_header(phase)}  |  ")
        if d["captured_at"]:
            h2.update(Text(
                money +
                f"snapshot {d['captured_at']} "
                f"({age_of(d['captured_at'])})"))
        else:
            h2.update(Text(money + "snapshot: nothing imported yet"))
        warn = "  anchors != guarantees"
        if self.error:
            warn = "  STALE -- reload failed"
        exp = d.get("experiment") or {}
        vsrc = (phase.get("value_model") or {}).get("source")
        term_h = exp.get("candidate_term_hours")
        if not phase:
            cand_val = "value=unavailable"
        elif vsrc in (None, "unavailable"):
            cand_val = (f"value=relative/{term_h}h"
                        if term_h else "value=relative")
        else:
            cand_val = (f"value=provisional/{term_h}h"
                        if term_h else "value=provisional")
        bal = d.get("balance_usd")
        ep_bits = []
        if phase.get("epoch_plan_profit_usd") is not None:
            ep_bits.append(
                f"EP profit~${phase['epoch_plan_profit_usd']}")
        if (phase.get("epoch_plan_usd") is not None
                and phase.get("epoch_plan_phase_limited")):
            # the projection runs to epoch end; the phase deadline
            # itself is unchanged
            ep_bits.append("EP needs phase extension")
        h3.update(Text(
            f"rows={d['total']} avail={d['available']} "
            f"ranked={d['ranked']} "
            f"noncuda-hidden={d.get('cuda_hidden', 0)} "
            f"{cand_val} "
            f"bal={'$' + format(bal, '.2f') if bal is not None else '-'}"
            f"{(' (' + age_of(d['balance_at']) + ')'
                ) if bal is not None else ''}"
            f"{'  ' + '  '.join(ep_bits) if ep_bits else ''} "
            f"src={kind}"
            f"{(' rld ' + self.reloaded_at.strftime('%H:%M:%S'))
                if self.reloaded_at else ''}{warn}"))

    # -- rendering ---------------------------------------------------------

    def _search_text(self):
        try:
            return self.query_one(Input).value.strip().lower()
        except Exception:
            return ""

    def _filtered(self):
        rows = self.dash["rows"] if self.dash else []
        text = self._search_text()
        if self.avail_only:
            rows = [r for r in rows if r.get("available")]
        if text:
            def hit(r):
                hay = " ".join(str(v or "") for v in (
                    r.get("server_id"), r.get("owner"), r.get("country"),
                    r.get("cpu"), r.get("gpu"), r.get("cpu_isa"),
                    r.get("ineligible_reason"),
                    " ".join(str(g) for g in
                             ((r.get("listing") or {}).get("gpu_array")
                              or []))))
                return text in hay.lower()
            rows = [r for r in rows if hit(r)]
        if self.sort_by_price:
            rows = sorted(rows, key=lambda r: (
                r.get("effective_price_day_usd") is None,
                r.get("effective_price_day_usd") or 0))
        elif any(r.get("forecast_net_hr") is not None for r in rows):
            # the runner's own ranking once calibrated: forecast net
            # $/h desc, then the same price/id tie-breaks _candidates
            # uses
            rows = sorted(rows, key=lambda r: (
                r.get("forecast_net_hr") is None,
                -(r.get("forecast_net_hr") or 0),
                r.get("hourly_cost_usd")
                or r.get("effective_price_day_usd") or 0,
                r.get("server_id") or 0))
        elif any("auto_prior" in r for r in rows):
            # relative value per $ when there is no dollar forecast
            rows = sorted(rows, key=lambda r: (
                r.get("auto_prior") is None,
                -(r.get("auto_prior") or 0),
                r.get("hourly_cost_usd")
                or r.get("effective_price_day_usd") or 0,
                r.get("server_id") or 0))
        else:
            rows = sorted(rows, key=lambda r: (
                r.get("est_its_per_usd") is None,
                -(r.get("est_its_per_usd") or 0)))
        return rows

    def render_tables(self):
        rows = self._filtered()
        cand = self.query_one("#cand", DataTable)
        mkt = self.query_one("#mkt", DataTable)
        self._fill(cand, [r for r in rows if r.get("eligible")], CAND_COLS,
                   "cand")
        self._fill(mkt, rows, MKT_COLS, "mkt")
        self.render_experiment()
        self.render_fleet()

    def render_experiment(self):
        """Read-only view of experiment.py's tables; the runner itself
        lives in a terminal (`experiment.py run`)."""
        exp = (self.dash or {}).get("experiment")
        status = self.query_one("#exp-status", Static)
        upgrade = self.query_one("#exp-upgrade", Static)
        table = self.query_one("#exp-table", DataTable)
        logw = self.query_one("#exp-log", TextArea)
        snap = self._snapshot(table)
        keep_log_scroll = self._keep_log_scroll()
        table.clear()
        self._autofit_reset(table)
        logw.load_text("")
        if not exp or not exp.get("phase"):
            status.update(Text("no experiment phase"))
            upgrade.update(Text(""))
            return
        ph = exp["phase"]
        alive = exp.get("runner_alive")
        runner = {True: "running", False: "NOT RUNNING"}.get(
            alive, "unknown")
        parts = [
            f"phase {ph['phase_id']}: {ph['started_at']} -> "
            f"{ph['ends_at']}",
            f"budget ${ph['budget']} committed ${ph['committed']} "
            f"remaining ${ph['remaining']}",
            f"runner: {runner}",
            f"loss mining {'on' if ph.get('loss_mining') else 'off'}",
            f"score={ph.get('score') or 'net'}"]
        if exp.get("pending_rerents"):
            parts.append(
                "rerents: " + ", ".join(
                    f"{p['server_id']} due {end_label(p['due_at'])}"
                    for p in exp["pending_rerents"]))
        for b in exp.get("bursts") or (
                [exp["burst"]] if exp.get("burst") else []):
            parts.append(
                f"burst {b['burst_id']} ({b['trigger']}) "
                f"{len(b['probes'])}/{b['n_target']} probes "
                f"judge in {int(b['judge_in_s'] // 60)}m")
        status.update(Text("  |  ".join(parts)))
        # pin the newest 'upgrade scan:' note (events arrive
        # newest-first): a dated assessment of the last comparison,
        # not a live ranking while a burst is in progress
        scan = next(
            (e for e in exp.get("events") or []
             if e.get("kind") == "note"
             and str((e.get("detail") or {}).get("text") or "")
             .startswith("upgrade scan:")), None)
        upgrade.update(Text(
            "Latest upgrade check: " + _ev_line(scan)
            if scan is not None
            else "Upgrade check: no assessment yet"))
        text = self._search_text()
        items = exp.get("rentals") or []
        if text:
            items = [r for r in items if text in " ".join(str(v or "")
                     for v in (r.get("server_id"), r.get("cpu"),
                               r.get("gpu"), r.get("role"),
                               r.get("tier"))).lower()]
        for r in items:
            table.add_row(
                plain(r.get("order_id")), plain(r.get("server_id")),
                plain(r.get("role")),
                plain("spot" if r.get("mode") == "spot" else "-"),
                plain(r.get("tier")),
                plain(trunc(cpu_compact(r.get("cpu")), 16)),
                plain(trunc(r.get("gpu"), 12)),
                plain(f"{float(r['hourly_usd']):.3f}"
                      if r.get("hourly_usd") is not None else "-"),
                plain(str(round(float(r["its_total"])))
                      if r.get("its_total") is not None else "-"),
                plain(f"~{float(r['forecast_return_x']):.2f}x"
                      if r.get("forecast_return_x") is not None
                      else "-"),
                plain(f"~${float(r['forecast_usd_hr']):.3f}"
                      if r.get("forecast_usd_hr") is not None
                      else "-"),
                plain(f"~${float(r['forecast_net_hr']):.3f}"
                      if r.get("forecast_net_hr") is not None
                      else "-"),
                plain(age_s_label(r.get("age_s"))),
                plain(r.get("shares")),
                plain(f"{r['shares_per_h']:.0f}"
                      if r.get("shares_per_h") is not None else "-"),
                plain(age_of(r.get("last_share_at"))
                      if r.get("last_share_at") else "-"),
                plain(trunc(r.get("tree_id"), 10)),
                plain(r.get("next") or "-"),
                key=str(r.get("order_id")))
        self._apply_sort(table)
        self._restore(table, snap)
        lines = [_ev_line(e) for e in exp.get("events") or []]
        keep_log_scroll("\n".join(lines))

    def _cells(self, r):
        return {
            "ID": plain(r.get("server_id")),
            "GPU": plain(trunc(gpu_label(r), 12)),
            "CPU": plain(cpu_compact(r.get("cpu")) + (
                " +512" if "AVX512" in str(r.get("cpu_isa") or "")
                else "")),
            "Price$/d": plain(fmt_price(r)),
            "OD$/d": plain(fmt_price(r)),
            "Mode": plain("spot" if r.get("mode") == "spot" else "-"),
            "Spot%": plain(
                f"-{round(r['spot_discount'] * 100)}%"
                if isinstance(r.get("spot_discount"), (int, float))
                and r["spot_discount"] > 0
                else "-"),
            "SpotFlr": plain(spot_floor_label(r)),
            "Avail": plain("yes" if r.get("available") else "no"),
            "CC": plain(r.get("country") or "--"),
            "Est$/h": plain(
                f"~${r['forecast_usd_hr']:.3f}"
                if r.get("forecast_usd_hr") is not None else "-"),
            "Net$/h": plain(
                f"~${r['forecast_net_hr']:.3f}"
                if r.get("forecast_net_hr") is not None else "-"),
            "EstRet": plain(
                f"~{r['forecast_return_x']:.2f}x"
                if r.get("forecast_return_x") is not None else "-"),
            "Auto": plain(r.get("value_fill_status") or "-"),
            "Upg": plain(r.get("upgrade_status") or "-"),
            "est it/s": plain(
                ((f"~" if r.get("est_extrapolated") else "")
                 + f"{r['est_its']:.0f}"
                 + ("*" if r.get("est_cross_epoch") else ""))
                if r.get("est_its") else "-"),
            "it/$": plain(
                f"{r['est_its_per_usd']:.0f}"
                if r.get("est_its_per_usd") else "-"),
            "thr": plain(r.get("threads_listed") or "?"),
            "CPU model": plain(r.get("cpu")),
            "ISA": plain(isa_label(r.get("cpu_isa"))),
            "Rel%": plain(fmt_rel(r)),
            "Reason": plain(r.get("ineligible_reason") or ""),
        }

    def _snapshot(self, table):
        """Cursor row key + scroll offsets, so a reload rebuild keeps
        the user's view instead of snapping to the top-left."""
        key = None
        rows = table.ordered_rows
        if rows and 0 <= table.cursor_row < len(rows):
            key = rows[table.cursor_row].key.value
        return (key, table.scroll_target_x, table.scroll_target_y)

    def _restore(self, table, snap):
        key, sx, sy = snap
        if key is not None:
            for i, row in enumerate(table.ordered_rows):
                if row.key.value == key:
                    table.move_cursor(row=i)
                    break
        if sx or sy:
            self.call_after_refresh(
                lambda: table.scroll_to(x=sx, y=sy, animate=False))

    def _keep_scroll(self, widget_id):
        """VerticalScroll regions reset to top on update; carry the
        offset across a content refresh."""
        try:
            sv = self.query_one(widget_id)
        except Exception:
            return lambda: None
        sy = sv.scroll_target_y
        return lambda: self.call_after_refresh(
            lambda: sv.scroll_to(y=sy, animate=False))

    def _keep_log_scroll(self):
        """Capture the exp-log view position before a text rewrite:
        restore it after, or keep pinned to the tail when already at
        the bottom (and on first fill). An active selection is
        re-applied so a refresh doesn't wipe a copy in progress."""
        try:
            w = self.query_one("#exp-log", TextArea)
        except Exception:
            return lambda _text: None
        sy = w.scroll_target_y
        at_bottom = w.max_scroll_y <= 0 or sy >= w.max_scroll_y - 0.5
        sel = w.selection
        def _apply(text):
            w.load_text(text)
            try:
                if sel and sel != w.selection:
                    w.selection = sel
            except Exception:
                pass
            if at_bottom:
                self.call_after_refresh(
                    lambda: w.scroll_end(animate=False))
            else:
                self.call_after_refresh(
                    lambda: w.scroll_to(y=sy, animate=False))
        return _apply

    @staticmethod
    def _autofit_reset(table):
        """Drop ratcheted column widths so auto_width columns re-fit
        the current rows: clear() keeps Column.content_width, and
        _update_dimensions only ever max()es it, so without this the
        widest cell ever seen pins the column forever."""
        for col in table.columns.values():
            col.content_width = len(col.label.plain)

    def _fill(self, table, rows, cols, which):
        selected = self.sel_id[which]
        snap = self._snapshot(table)
        table.clear()
        self._autofit_reset(table)
        sel_row = None
        for i, r in enumerate(rows):
            cells = self._cells(r)
            dim = (not r.get("available")
                   or r.get("ineligible_reason") == "blocked country")
            table.add_row(*(_dim(cells[col]) if dim else cells[col]
                            for col in cols),
                          key=str(r.get("server_id")))
            if selected is not None and r.get("server_id") == selected:
                sel_row = i
        if sel_row is not None:
            table.move_cursor(row=sel_row)
        self._apply_sort(table)
        self._restore(table, snap)

    def _apply_sort(self, table):
        """Re-run the clicked-column sort after a table rebuild."""
        spec = self.sort_cols.get(table.id)
        if spec:
            table.sort(spec[0],
                       key=lambda cell: _cell_sort_key(cell, spec[1]))

    def render_fleet(self):
        fleet = (self.dash or {}).get("fleet") or []
        fleet_all = (self.dash or {}).get("fleet_all") or fleet
        status = self.query_one("#fleet-status", Static)
        table = self.query_one("#fleet-table", DataTable)
        active_ids = {r.get("order_id") for r in fleet}
        items = fleet_all if self.fleet_show_all else fleet
        text = self._search_text()
        if text:
            items = [r for r in items if text in " ".join(str(v or "")
                     for v in (r.get("order_id"), r.get("server_id"),
                               r.get("alias"), r.get("pool"),
                               (r.get("status") or {}).get("alias"),
                               (r.get("status") or {}).get("pool"),
                               r.get("health"),
                               (r.get("status") or {}).get("hardware",
                               {}).get("cpu_model"),
                               " ".join((r.get("status") or {}).get(
                                   "hardware", {}).get("gpus") or []),
                               )).lower()]
        if not fleet_all:
            status.update(Text(
                "No rigs enrolled; `rent.py create` or `rent.py enroll` "
                "adds them here (read-only)."))
        else:
            latest = max((r.get("attempted_at") or "" for r in fleet_all),
                         default="")
            rates = [r["rate_hr_usd"] for r in fleet
                     if r.get("rate_hr_usd") is not None]
            spends = [r["spend_usd"] for r in items
                      if r.get("spend_usd") is not None]
            parts = ([f"burn ${sum(rates):.3f}/hr"] if rates else []) + \
                    ([f"spend ${sum(spends):.2f}"] if spends else [])
            totals = "  ".join(parts) or "burn/spend n/a"
            hidden = len(fleet_all) - len(fleet)
            view = ("all" if self.fleet_show_all else
                    f"active ({hidden} ended hidden)" if hidden
                    else "active")
            status.update(Text(
                f"{len(items)} rig(s) [{view} -- f toggles] -- "
                f"last poll {latest or 'never'} "
                f"({age_of(latest) if latest else 'unknown age'}); "
                f"unreachable rigs keep last-good rates below; "
                f"End* = auto-renewing term  |  {totals}"))
        snap = self._snapshot(table)
        table.clear()
        self._autofit_reset(table)
        for r in items:
            st = r.get("status") or {}
            its = st.get("its") or {}
            shares = st.get("shares") or {}
            trainer = st.get("trainer") or {}
            cells = [
                plain(r.get("server_id") or "?"),
                plain(st.get("pool") or r.get("pool") or "?"),
                plain(r.get("health") or "unknown"),
                plain(rate_fmt(its.get("cpu"))),
                plain(rate_fmt(its.get("gpu"))),
                plain(shares.get("epoch")
                      if shares.get("epoch") is not None else "-"),
                plain(shares.get("found")
                      if shares.get("found") is not None else "-"),
                plain(age_of(r.get("observed_at"))
                      if r.get("observed_at") else "-"),
                plain(f"{r['rate_hr_usd']:.3f}"
                      if r.get("rate_hr_usd") is not None else "-"),
                plain(f"{r['shares_hr']:.1f}"
                      if r.get("shares_hr") is not None else "-"),
                plain(f"{r['shares_hr'] / (r['rate_hr_usd'] * 100):.2f}"
                      if r.get("shares_hr") is not None
                      and r.get("rate_hr_usd") else "-"),
                plain(f"{r['spend_usd']:.2f}"
                      if r.get("spend_usd") is not None else "-"),
                plain("yes" if r.get("online_reported") is True else
                      "no" if r.get("online_reported") is False else "?"),
                plain(r.get("control") or "-"),
                plain("spot" if r.get("mode") == "spot" else "-"),
                plain(uptime_label(r.get("created_at"),
                                   r.get("up_until"))),
                plain(end_label(r.get("end_at"))
                      + ("*" if r.get("auto_renew") else "")),
                plain(r.get("order_id")),
                plain(rig_gpu(r)),
                plain(trunc(st.get("alias") or r.get("alias"), 16)),
                plain(shares.get("submitted")
                      if shares.get("submitted") is not None else "-"),
                plain(" ".join(x for x in (
                    trainer.get("variant"), trainer.get("version"))
                    if x) or "-")]
            if r.get("order_id") not in active_ids:
                cells = [_dim(c) for c in cells]
            table.add_row(*cells, key=str(r.get("order_id")))
        self._apply_sort(table)
        self._restore(table, snap)

    # -- input -------------------------------------------------------------

    def on_input_changed(self, event):
        self.fingerprint = None
        self.render_tables()

    def on_input_submitted(self, event):
        # Enter in the filter = done typing; same as Escape
        self.action_leave_search()

    async def action_quit(self):
        if self._rent_active:
            self.notify("rental in progress -- quit blocked until it "
                        "finishes (b reopens the log)",
                        severity="warning")
            return
        # modal screens have their own close keys; a stray `q` while
        # a form field is unfocused must not kill the app
        if len(self.screen_stack) != 1:
            return
        await super().action_quit()

    def action_focus_search(self):
        if len(self.screen_stack) != 1:
            return
        self._search_pinned = True
        self.query_one("#search").display = True
        self.query_one(Input).focus()

    def action_reset_filters(self):
        if len(self.screen_stack) != 1:
            return
        self.query_one(Input).value = ""
        self.avail_only = False
        self.sort_by_price = False
        self.fleet_show_all = False
        self.fingerprint = None
        self.render_tables()
        self.notify("filters reset")

    def action_help(self):
        if len(self.screen_stack) != 1:
            return
        self.push_screen(HelpModal(self.db_path))

    def action_rent_selected(self):
        # only on the default screen; modals swallow it themselves
        if len(self.screen_stack) != 1:
            return
        if self._rent_active and self._rent_modal is not None:
            # progress view was hidden -- reopen a fresh modal on the
            # same log rather than starting a second rental
            modal = ProgressModal(title=self._rent_modal._title)
            modal._lines = list(self._rent_log)
            self._rent_modal = modal
            self.push_screen(modal)
            return
        pane = self._active_pane()
        if pane not in ("candidates", "marketplace"):
            return
        table = self.query_one("#cand" if pane == "candidates"
                               else "#mkt", DataTable)
        try:
            row_key = table.ordered_rows[table.cursor_row].key.value
            server_id = int(row_key)
        except (IndexError, TypeError, ValueError):
            return
        self._rent_server(server_id)

    def _rent_server(self, server_id):
        dash_row = next(
            (r for r in (self.dash["rows"] if self.dash else [])
             if r.get("server_id") == server_id), None)
        if dash_row is None:
            try:
                detail = read_server_details(self.db_path, server_id)
            except DashboardError:
                detail = None
            if detail is not None:
                dash_row = {"server_id": server_id,
                            "listing": detail["listing"],
                            "available": available(detail["listing"])}
        if dash_row is None:
            self.notify(f"server {server_id} not in this snapshot",
                        severity="warning")
            return
        if not dash_row.get("available"):
            self.notify(f"server {server_id} is not currently "
                        "available (cached row)", severity="warning")
            return
        if self._rent_active:
            self.notify("rental already in progress",
                        severity="warning")
            return
        # the dismiss callback is delivered via the pump that pushed
        # the screen -- force it to the app pump (always alive), not a
        # modal that may already be gone when the result arrives
        from textual._context import active_message_pump
        token = active_message_pump.set(self)
        try:
            self.push_screen(RentalSetup(dash_row), self._rent_done)
        finally:
            active_message_pump.reset(token)

    def _focus_pane_table(self):
        """Return focus to the active pane's table after a modal
        closes -- otherwise `b`/`l` can land in a leftover Input."""
        pane = self._active_pane()
        table_id = {"candidates": "#cand", "marketplace": "#mkt",
                    "experiment": "#exp-table",
                    "fleet": "#fleet-table"}.get(pane, "#cand")
        try:
            self.query_one(table_id, DataTable).focus()
        except Exception:
            pass

    def _rent_done(self, result):
        if result is None:
            self.call_after_refresh(self._focus_pane_table)
            return
        self._rent_active = True
        self._rent_log = []
        row = next(
            (r for r in (self.dash["rows"] if self.dash else [])
             if r.get("server_id") == result.get("server_id")), None)
        # quote phase does several live API GETs -- show progress
        # immediately so the wait isn't silent
        modal = ProgressModal(
            title="fetching fresh quote -- live API checks")
        self._rent_modal = modal
        self.push_screen(modal)
        self._rent_line("quoting: marketplace + fees + orders...")
        self.call_after_refresh(
            lambda: self._rent_quote(result, row or {}))

    def _rent_line(self, msg):
        line = f"{datetime.now().strftime('%H:%M:%S')} {msg}"
        self._rent_log.append(line)
        modal = self._rent_modal
        if modal is not None and hasattr(modal, "add_line"):
            modal.add_line(line)

    @work(thread=True, exclusive=True, group="rent")
    def _rent_quote(self, result, row):
        """Phase 1 in-process: rent.quote_order (fresh guarded GETs,
        no lock held). On success shows the ReviewModal."""
        import rent

        def collect(msg):
            self.call_from_thread(self._rent_line, str(msg))

        try:
            ctx = rent.quote_order(
                server_id=result["server_id"], hours=result["hours"],
                auto_renew=bool(result.get("auto_renew")),
                max_cost_usd=result.get("max_cost_usd"),
                pool=result["pool"],
                cpu_threads=result.get("cpu_threads"),
                db_path=os.path.expanduser(self.db_path),
                env_file=DEFAULT_ENV, key_path=rent.DEFAULT_KEY,
                say=collect, warn=collect)
        except (ValueError, rent.APIError, OSError) as exc:
            self.call_from_thread(
                self.notify, f"rent quote failed: {exc}",
                severity="error")
            self.call_from_thread(self._rent_quote_failed)
            return
        except Exception as exc:
            # never surface raw exception text that could carry secrets
            self.call_from_thread(
                self.notify,
                f"rent quote failed: {type(exc).__name__}",
                severity="error")
            self.call_from_thread(self._rent_quote_failed)
            return
        self.call_from_thread(self._rent_quoted, ctx, row)

    def _rent_quote_failed(self):
        self._rent_active = False
        modal = self._rent_modal
        self._rent_modal = None
        if modal is not None:
            modal.finish("quote failed -- nothing placed")
        self.call_after_refresh(self._focus_pane_table)

    def _rent_quoted(self, ctx, row):
        modal = self._rent_modal
        self._rent_modal = None
        if modal is not None and modal in self.screen_stack:
            modal.dismiss()
        self.push_screen(
            ReviewModal(row, ctx,
                        on_confirm=self._rent_confirmed,
                        on_cancel=self._rent_review_cancelled))

    def _rent_review_cancelled(self):
        self._rent_active = False
        self.call_after_refresh(self._focus_pane_table)
        self.notify("rent cancelled -- nothing placed",
                    severity="info")

    def _rent_confirmed(self, ctx):
        """Explicit Confirm & Rent click: open the progress view and
        run phase 2 (create_order_flow) in a worker thread."""
        modal = ProgressModal()
        self._rent_modal = modal
        self.push_screen(modal)
        self._rent_line("confirmed -- re-verifying terms, then ONE "
                        "create_order POST (no retries)")
        self._rent_create(ctx)

    @work(thread=True, exclusive=True, group="rent")
    def _rent_create(self, ctx):
        """Phase 2 in-process: the exact guarded create flow (operator
        lock, fresh re-verify, ONE POST, auto-cancel rules)."""
        import rent

        def collect(msg):
            self.call_from_thread(self._rent_line, str(msg))

        try:
            rc, info = rent.create_order_flow(
                ctx, say=collect, warn=collect)
        except Exception as exc:
            # create_order_flow returns on every handled path; this is
            # a last-resort guard -- type name only, never raw text.
            rc, info = 1, {"order_id": None, "provision_state": None,
                           "stage": type(exc).__name__}
        self.call_from_thread(self._rent_finished, rc, info)

    def _rent_finished(self, rc, info):
        info = info or {}
        modal = self._rent_modal
        self._rent_modal = None
        self._rent_active = False
        order_id = info.get("order_id")
        state = info.get("provision_state")
        stage = info.get("stage")
        if rc == 0:
            if modal is not None:
                modal.finish(f"done: order {order_id} READY")
            self.notify(f"order {order_id} READY", severity="info")
        else:
            if modal is not None:
                modal.finish(
                    f"finished rc={rc} stage={stage} "
                    f"state={state or '-'}")
            msg = f"rent flow ended rc={rc}"
            if order_id is not None:
                msg += f" order {order_id}"
            if state:
                msg += f" state {state}"
            if self._rent_log:
                msg += f" -- {self._rent_log[-1][9:][:120]}"
            self.notify(msg, severity="warning")
        self.call_after_refresh(self._focus_pane_table)
        self.reload_data()

    def action_log_selected(self):
        """`l` on a Fleet or Experiment row: read-only SSH tail; the
        archived copy is shown when the rig can't be reached."""
        if len(self.screen_stack) != 1:
            return
        pane = self._active_pane()
        if pane not in ("fleet", "experiment"):
            return
        table = self.query_one(
            "#fleet-table" if pane == "fleet" else "#exp-table",
            DataTable)
        try:
            row_key = table.ordered_rows[table.cursor_row].key.value
        except (IndexError, TypeError):
            return
        rigs = (self.dash or {}).get("fleet_all") or \
            (self.dash or {}).get("fleet") or []
        rig = next((r for r in rigs
                    if str(r.get("order_id")) == str(row_key)), None)
        if rig is not None and rig.get("unmanaged"):
            self.notify("unmanaged order -- no rig telemetry",
                        severity="warning")
            return
        if rig is None:
            if pane == "experiment":
                self.notify("no fleet record for that order",
                            severity="warning")
            return
        if not rig.get("ssh_host") or not rig.get("ssh_port"):
            self.notify("rig has no SSH endpoint recorded",
                        severity="warning")
            return
        st = rig.get("status") or {}
        pool = st.get("pool") if st.get("pool") in ("jetski", "qli") \
            else rig.get("pool")
        if pool not in ("jetski", "qli"):
            self.notify("pool unknown -- cannot pick log file",
                        severity="warning")
            return
        self.push_screen(
            LogModal(rig, pool, db_path=self.db_path),
            lambda _result: self.call_after_refresh(
                self._focus_pane_table))

    def action_leave_search(self):
        if isinstance(self.focused, Input):
            # re-hide the filter bar; the typed filter still applies
            self._search_pinned = False
            self.query_one("#search").display = False
            pane = self._active_pane()
            table_id = {"candidates": "#cand", "marketplace": "#mkt",
                        "experiment": "#exp-table",
                        "fleet": "#fleet-table"}.get(pane, "#cand")
            try:
                self.query_one(table_id, DataTable).focus()
            except Exception:
                pass

    def _active_pane(self):
        try:
            return self.query_one(TabbedContent).active
        except Exception:
            return "candidates"

    def check_action(self, action, parameters):
        """Hide tab-scoped footer keys on tabs where they do nothing."""
        panes = PANE_ACTIONS.get(action)
        if panes is None:
            return True
        # False = disabled + removed from the footer
        return self._active_pane() in panes

    def on_tabbed_content_tab_activated(self, _event):
        self.refresh_bindings()

    def action_toggle_available(self):
        if len(self.screen_stack) != 1:
            return
        self.avail_only = not self.avail_only
        self.fingerprint = None
        self.render_tables()
        self.notify(f"available-only: {self.avail_only}")

    def action_toggle_fleet_all(self):
        if len(self.screen_stack) != 1:
            return
        self.fleet_show_all = not self.fleet_show_all
        self.fingerprint = None
        self.render_fleet()
        self.notify("fleet: all rigs (incl. ended)" if self.fleet_show_all
                    else "fleet: active rigs only")

    def action_toggle_sort(self):
        if len(self.screen_stack) != 1:
            return
        self.sort_by_price = not self.sort_by_price
        self.fingerprint = None
        self.render_tables()
        self.notify("sort: fee-inclusive $/day asc" if self.sort_by_price
                    else "sort: forecast return / value desc")

    def on_data_table_header_selected(self, event):
        """Click a column header to sort ascending; again to reverse."""
        table, col = event.data_table, event.column_key
        prev = self.sort_cols.get(table.id)
        reverse = prev == (col, False)
        self.sort_cols[table.id] = (col, reverse)
        self._apply_sort(table)
        for key, column in table.columns.items():
            base = column.label.plain.rstrip(" ▲▼")
            arrow = "▼" if key == col and reverse else \
                    "▲" if key == col else ""
            column.label = Text(base + arrow)
        table.refresh()

    def on_data_table_row_selected(self, event):
        table = event.data_table
        row = event.row_key.value if event.row_key else None
        if row is None:
            return
        if table.id == "exp-table":
            rig = next((r for r in (self.dash or {}).get("fleet_all") or []
                        if str(r.get("order_id")) == row), None)
            if rig is not None:
                self.push_screen(FleetDetail(rig, self.db_path))
            else:
                self.notify("no fleet record for that order",
                            severity="warning")
            return
        if table.id == "fleet-table":
            rig = next((r for r in (self.dash or {}).get("fleet_all") or []
                        if str(r.get("order_id")) == row), None)
            if rig is not None:
                if rig.get("unmanaged"):
                    order = next(
                        (o for o in (self.dash or {}).get(
                            "order_history") or []
                         if str(o.get("order_id")) == row), None)
                    if order is not None:
                        self.push_screen(OrderDetail(order))
                    return
                self.push_screen(FleetDetail(rig, self.db_path))
            return
        which = "cand" if table.id == "cand" else "mkt"
        self.sel_id[which] = int(row)
        dash_row = next(
            (r for r in (self.dash["rows"] if self.dash else [])
             if r.get("server_id") == self.sel_id[which]), None)
        try:
            detail = read_server_details(self.db_path, self.sel_id[which])
        except DashboardError as exc:
            self.notify(str(exc), severity="error")
            return
        self.push_screen(ServerDetail(detail, dash_row or {}))

    def on_data_table_row_highlighted(self, event):
        if event.data_table.id not in ("cand", "mkt"):
            return  # fleet/experiment tables have their own detail paths
        if event.row_key:
            which = "cand" if event.data_table.id == "cand" else "mkt"
            try:
                self.sel_id[which] = int(event.row_key.value)
            except (TypeError, ValueError):
                pass

    def on_button_pressed(self, event):
        """◀/▶ side buttons: nudge the table one column left/right."""
        btn_id = event.button.id or ""
        left = btn_id.endswith("-left")
        if not (left or btn_id.endswith("-right")):
            return
        table_id = btn_id.rsplit("-", 1)[0]
        try:
            table = self.query_one(f"#{table_id}", DataTable)
        except Exception:
            return
        self._nudge_x(table, -1 if left else 1)
        table.focus()

    def _nudge_x(self, table, direction):
        """Scroll horizontally to the next column boundary."""
        x = table._row_label_column_width
        edges = []
        for column in table.ordered_columns:
            edges.append(x)
            x += column.get_render_width(table)
        target = table.scroll_target_x
        if direction > 0:
            new = next((e for e in edges if e > target + 0.5),
                       table.max_scroll_x)
        else:
            new = next((e for e in reversed(edges) if e < target - 0.5), 0)
        table.scroll_to(x=new)

    def on_mouse_down(self, event):
        """Arm a possible drag scroll (content follows the pointer)."""
        self._drag_scroll = None
        try:
            widget, _ = self.screen.get_widget_at(event.screen_x,
                                                  event.screen_y)
        except Exception:
            return
        if isinstance(widget, (Input, TextArea, ScrollBar)):
            return  # these use drags for selection / scrollbar moves
        # nearest overflowing ancestor per axis; x and y may differ
        x_widget = y_widget = None
        node = widget
        while isinstance(node, Widget):
            if (x_widget is None and node.allow_horizontal_scroll
                    and node.max_scroll_x > 0):
                x_widget = node
            if (y_widget is None and node.allow_vertical_scroll
                    and node.max_scroll_y > 0):
                y_widget = node
            node = node.parent
        if x_widget or y_widget:
            self._drag_scroll = (
                x_widget, y_widget, event.screen_x, event.screen_y,
                x_widget.scroll_target_x if x_widget else 0,
                y_widget.scroll_target_y if y_widget else 0)

    def on_mouse_move(self, event):
        if not self._drag_scroll or event.button != 1:
            return
        x_widget, y_widget, x0, y0, sx0, sy0 = self._drag_scroll
        if x_widget is not None:
            x_widget.scroll_to(x=sx0 - (event.screen_x - x0),
                               animate=False)
        if y_widget is not None:
            y_widget.scroll_to(y=sy0 - (event.screen_y - y0),
                               animate=False)

    def on_mouse_up(self, _event):
        self._drag_scroll = None


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Marketplace TUI (local SQLite; `b` runs the "
                    "guarded in-TUI rent flow, `l` tails a rig's "
                    "miner log once).")
    parser.add_argument("--db", default=DEFAULT_DB, help="SQLite path")
    parser.add_argument("--refresh", type=float, default=5,
                        help="db reread interval seconds (>=1, default 5)")
    args = parser.parse_args(argv)
    if not math.isfinite(args.refresh) or args.refresh < 1:
        parser.error("--refresh must be a finite number >= 1 second")
    app = MarketTui(db_path=args.db, refresh=args.refresh)
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
