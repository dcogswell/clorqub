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
#bench { height: auto; padding: 0 1; }
#exp-scroll { height: 10; }
#exp-log { height: auto; padding: 0 1; }
.compact #search { display: none; }
.compact #hdr { height: 2; }
.compact #h3 { display: none; }
.compact Button.nudge { width: 3; min-width: 3; }
ServerDetail, HelpModal, OrderDetail, FleetDetail, RentalSetup,
ReviewModal, ProgressModal, LogModal, RenewModal {
    align: center middle; }
#rental .rfield { height: 3; }
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

WARN_LINES = [
    "Provisional shortlist, not a rental approval. The sols/h column is a "
    "reference-rig anchor (~160 approximate; >350 source lower bound), not a "
    "guaranteed floor for any candidate.",
    "Reference CPU allocation, epoch and trainer version are unknown; equal "
    "allocations are not assumed.",
    "OD$/d includes an assumed 5% renter fee (prior observation, not "
    "refreshed); creation fee and boot opportunity cost are omitted.",
    "SpotFlr = observed fee-inclusive spot floor (USD-Blockchain only, "
    "1.25% fee assumed); '*' = offers present, '-' = not collected/stale. "
    "Indicative only -- not a winning-bid price or execution guarantee; "
    "ranking stays on-demand.",
    "No QUBIC conversion; CPU and GPU it/s are not additive.",
    "Clore-reported spend/online columns are observe-only (including "
    "unmanaged web-UI rentals); reported spend is cumulative, not "
    "epoch-verified.",
]


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


def plain(value):
    return Text("" if value is None else str(value))


def _dim(cell):
    cell.stylize("dim")
    return cell


def yield_marker(row):
    sols = row.get("estimated_sols_hr")
    if sols is None:
        return "n/a"
    if row.get("benchmark_kind") == "lower_bound":
        return f">{sols:.0f}"
    return f"~{sols:.0f}"


CAND_COLS = ("ID", "GPU", "OD$/d", "SpotFlr", "sols/h", "ODsol/$", "CC",
             "thr", "CPU model", "ISA", "Rel%")
MKT_COLS = ("ID", "GPU", "CPU", "Price$/d", "SpotFlr", "Avail", "CC",
            "sols/h", "ODsol/$", "thr", "CPU model", "ISA", "Rel%",
            "Reason")
EXP_COLS = ("Order", "Server", "Role", "Tier", "CPU", "GPU", "$/hr",
            "Age", "Shares", "Last share", "Restarts", "Tree", "Next")
FLEET_COLS = ("Server", "Pool", "Health", "CPU it/s", "GPU it/s",
              "Epoch", "Found", "Age", "$/hr", "Sh/h", "Spend$",
              "Clore$", "Online", "Control", "Up",
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


def fmt_ratio(row):
    ratio = row.get("estimated_sols_per_usd")
    return f"{ratio:.1f}" if ratio is not None else "-"


def fmt_rel(row):
    return rel_percent(row.get("reliability"))


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
        lines = [
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
            self._kv("source", r.get("benchmark_source") or "-"),
            self._kv("kind", r.get("benchmark_kind") or "-"),
            self._kv("reason", r.get("ineligible_reason")
                     or "benchmark-matched"),
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
  /            focus search filter (reveals the bar on small screens;
               Enter/Escape hides it again -- the filter still applies)
  b            rent setup -> in-TUI quote review -> Confirm & Rent
             (no terminal needed; nothing spends until you confirm)
  l            (on Fleet or Experiment) tail the rig's miner log -- one
             explicit read-only SSH fetch, no polling; ended rigs show
             the locally archived copy instead
  a            toggle available-only
  s            toggle sort: sols/$ desc <-> $/day asc
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
  Clore$ and Online are reported (not verified) order fields from the
  last successful Clore poll; Control is the order's control field.
  Clore orders never enrolled as rigs (manual web-UI rentals, etc.)
  appear as 'unmanaged' rows with '-' telemetry cells -- Enter shows
  their reported order record."""


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
            self._kv("  restarts", miner.get("restarts")),
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
            import clipboard
            cmd = self._ssh_cmd()
            tool = clipboard.copy_text(cmd)
            try:
                self.app.copy_to_clipboard(cmd)  # OSC 52 fallback (e.g. SSH)
            except Exception:
                pass
            if tool:
                self.app.notify(f"ssh command copied ({tool})")
            else:
                self.app.notify(
                    "sent via terminal escape; if paste fails your "
                    "terminal blocks OSC 52 -- install xclip/wl-copy",
                    severity="warning")

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
                Text("\n".join(self._lines[-400:])))
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
                    f"last captured {archive['last_captured_at']}"))
                log.update(Text("\n".join(
                    archive["text"].splitlines()[-self.lines:])))
            else:
                status.update(Text(f"fetch failed: {err}"))
                log.update(Text("(no log)"))
            return
        stamp = datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        status.update(Text(
            f"fetched {stamp} UTC -- {len(text)} chars"))
        log.update(Text(text if text else "(empty log)"))

    def on_button_pressed(self, event):
        if event.button.id == "l-refresh":
            self.query_one("#lstatus", Static).update(
                Text("fetching..."))
            self._fetch()
        else:
            self.dismiss()

    def action_close(self):
        self.dismiss()


class MarketTui(App):
    TITLE = "clorqub marketplace"
    CSS = CSS
    # drags scroll (touch-style); copying still works via the terminal's
    # own selection
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
        self._search_pinned = False
        self._drag_scroll = None
        # gentler horizontal pointer/trackpad scrolling (default is 4.0)
        self.scroll_sensitivity_x = 2.0

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
            with TabPane("Benchmarks", id="benchmarks"):
                yield VerticalScroll(Static(id="bench"))
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
                    with Horizontal(classes="twrap"):
                        yield Button("◀", id="exp-table-left",
                                     classes="nudge")
                        yield DataTable(id="exp-table",
                                        cursor_type="row")
                        yield Button("▶", id="exp-table-right",
                                     classes="nudge")
                    with VerticalScroll(id="exp-scroll"):
                        yield Static("", id="exp-log")
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
        `/` still reveals the search row via _search_pinned."""
        size = size or self.screen.size
        compact = size.height < 15 or size.width < 50
        if not compact:
            self._search_pinned = False
        self.set_class(compact, "compact")
        try:
            self.query_one("#search").display = (
                self._search_pinned if compact else True)
        except Exception:
            pass

    # -- data --------------------------------------------------------------

    def action_reload(self):
        self.reload_data()

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
                  (dash.get("order_inventory") or {}).get("revision"),
                  dash.get("fleet_signature"),
                  dash.get("experiment_signature"),
                  self.avail_only, self.sort_by_price,
                  self.fleet_show_all,
                  self._search_text())
            if fp != self.fingerprint:
                self.fingerprint = fp
                self.render_tables()
                self.render_benchmarks()
            self.update_header()
        else:
            self.update_header()
            if self.dash is None:
                self.render_tables()
                self.render_benchmarks()
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
        if d["captured_at"]:
            h2.update(Text(
                f"CACHED SNAPSHOT  imported {d['captured_at']} "
                f"({age_of(d['captured_at'])})"))
        else:
            h2.update(Text("CACHED SNAPSHOT  nothing imported yet"))
        warn = "  anchors != guarantees"
        if self.error:
            warn = "  STALE -- reload failed"
        bal = d.get("balance_usd")
        h3.update(Text(
            f"rows={d['total']} avail={d['available']} "
            f"ranked={d['ranked']} "
            f"noncuda-hidden={d.get('cuda_hidden', 0)} "
            f"bal={'$' + format(bal, '.2f') if bal is not None else '-'}"
            f"{(' (' + age_of(d['balance_at']) + ')'
                ) if bal is not None else ''} "
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
        else:
            rows = sorted(rows, key=lambda r: (
                r.get("estimated_sols_per_usd") is None,
                -(r.get("estimated_sols_per_usd") or 0)))
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
        table = self.query_one("#exp-table", DataTable)
        logw = self.query_one("#exp-log", Static)
        table.clear()
        logw.update(Text(""))
        if not exp or not exp.get("phase"):
            status.update(Text("no experiment phase"))
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
            f"runner: {runner}"]
        if exp.get("pending_rerents"):
            parts.append(
                "rerents: " + ", ".join(
                    f"{p['server_id']} due {end_label(p['due_at'])}"
                    for p in exp["pending_rerents"]))
        if exp.get("burst"):
            b = exp["burst"]
            parts.append(
                f"burst {b['burst_id']} ({b['trigger']}) "
                f"{len(b['probes'])}/{b['n_target']} probes "
                f"judge in {int(b['judge_in_s'] // 60)}m")
        status.update(Text("  |  ".join(parts)))
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
                plain(r.get("role")), plain(r.get("tier")),
                plain(trunc(cpu_compact(r.get("cpu")), 16)),
                plain(trunc(r.get("gpu"), 12)),
                plain(f"{float(r['hourly_usd']):.3f}"
                      if r.get("hourly_usd") is not None else "-"),
                plain(age_s_label(r.get("age_s"))),
                plain(r.get("shares")),
                plain(age_of(r.get("last_share_at"))
                      if r.get("last_share_at") else "-"),
                plain(r.get("restarts")),
                plain(trunc(r.get("tree_id"), 10)),
                plain(r.get("next") or "-"),
                key=str(r.get("order_id")))
        self._apply_sort(table)
        lines = []
        for e in exp.get("events") or []:
            try:
                hhmmss = datetime.fromisoformat(
                    e["ts"].replace("Z", "+00:00")).strftime("%H:%M:%S")
            except (ValueError, TypeError):
                hhmmss = str(e.get("ts") or "")[:8]
            detail = e.get("detail") or {}
            extras = " ".join(
                f"{k}={detail[k]}" for k in (
                    "reason", "tier", "x_min", "y_min", "tree_id_before",
                    "n", "trigger", "type") if k in detail)
            lines.append(
                f"{hhmmss} {e.get('kind')}"
                f"{' ' + str(e['order_id']) if e.get('order_id') else ''}"
                f"{' s' + str(e['server_id']) if e.get('server_id') else ''}"
                f"{' ' + extras if extras else ''}")
        logw.update(Text("\n".join(lines)))

    def _cells(self, r):
        return {
            "ID": plain(r.get("server_id")),
            "GPU": plain(trunc(gpu_label(r), 12)),
            "CPU": plain(cpu_compact(r.get("cpu")) + (
                " +512" if "AVX512" in str(r.get("cpu_isa") or "")
                else "")),
            "Price$/d": plain(fmt_price(r)),
            "OD$/d": plain(fmt_price(r)),
            "SpotFlr": plain(spot_floor_label(r)),
            "Avail": plain("yes" if r.get("available") else "no"),
            "CC": plain(r.get("country") or "--"),
            "sols/h": plain(yield_marker(r)),
            "ODsol/$": plain(fmt_ratio(r)),
            "thr": plain(r.get("threads_listed") or "?"),
            "CPU model": plain(r.get("cpu")),
            "ISA": plain(isa_label(r.get("cpu_isa"))),
            "Rel%": plain(fmt_rel(r)),
            "Reason": plain(r.get("ineligible_reason") or "matched"),
        }

    def _fill(self, table, rows, cols, which):
        selected = self.sel_id[which]
        table.clear()
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
        table.clear()
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
                plain(f"{r['spend_usd']:.2f}"
                      if r.get("spend_usd") is not None else "-"),
                plain(f"{r['spend_reported_native']:.2f}"
                      if r.get("spend_reported_native") is not None
                      else "-"),
                plain("yes" if r.get("online_reported") is True else
                      "no" if r.get("online_reported") is False else "?"),
                plain(r.get("control") or "-"),
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

    def render_benchmarks(self):
        bench = self.query_one("#bench", Static)
        if self.dash is None:
            bench.update(Text("no data loaded"))
            return
        lines = [f"snapshot source: {self.dash['source']}",
                 "stored scoring anchors (performance_benchmarks)", ""]
        for b in self.dash["benchmarks"]:
            hi = b.get("max_sols_hr")
            rng = f"{b['min_sols_hr']:.0f}-{hi:.0f}" if hi else (
                f">{b['min_sols_hr']:.0f}" if b.get("observation_kind")
                == "lower_bound" else f"~{b['min_sols_hr']:.0f}")
            lines.append(
                f"{b['gpu_model']:<12} {b['cpu_profile']:<26} "
                f"{b['cpu_isa']:<7} pool={b['pool']} "
                f"kind={b['observation_kind']} sols/h={rng}")
            lines.append(
                f"    source={b['source']} threads={b.get('thread_count')}"
                f" epoch={b.get('epoch')} trainer={b.get('trainer_version')}")
            lines.append(f"    {b.get('notes')}")
        lines += ["", "context observations (provenance only -- NOT scored)",
                  ""]
        for o in self.dash["observations"]:
            lines.append(
                f"{o.get('id')}: device={o.get('device')} "
                f"gpu={o.get('gpu_model')} its={o.get('its')} "
                f"qualifier={o.get('qualifier')}")
            if o.get("notes"):
                lines.append(f"    {o['notes']}")
        lines += ["", "warnings", ""] + [f"- {w}" for w in WARN_LINES]
        bench.update(Text("\n".join(lines)))

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
        if self.has_class("compact"):
            # phone-sized viewport: reveal the hidden filter bar
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
            if self.has_class("compact"):
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
                    else "sort: sols/$ desc")

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
