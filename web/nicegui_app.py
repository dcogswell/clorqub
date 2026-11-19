#!/usr/bin/env python3
"""NiceGUI demo: marketplace + fleet from the clorqub sqlite db.

Run:  .venv/bin/pip install nicegui
      .venv/bin/python web/nicegui_app.py --db ~/.local/share/clorqub/clorqub.sqlite3
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nicegui import ui

from tui_data import read_dashboard

parser = argparse.ArgumentParser()
parser.add_argument("--db", required=True)
DB = parser.parse_known_args()[0].db


def market_rows():
    return [
        {
            "server_id": r["server_id"],
            "gpu": r["gpu"] or "—",
            "cpu": r["cpu"],
            "cc": r["country"],
            "usd_hr": round(r["hourly_cost_usd"], 3) if r["hourly_cost_usd"] else None,
            "est_sol_hr": r["estimated_sols_hr"],
            "sol_per_usd": r["estimated_sols_per_usd"],
        }
        for r in read_dashboard(DB)["rows"]
        if r["available"]
    ]


def fleet_rows():
    return [
        {
            "order_id": r["order_id"],
            "alias": r.get("alias") or "—",
            "pool": r.get("pool"),
            "health": r.get("health"),
            "state": r.get("provision_state"),
            "shares_hr": r.get("shares_hr"),
        }
        for r in read_dashboard(DB)["fleet"]
    ]


ui.dark_mode().enable()
ui.label("clorqub — marketplace").classes("text-2xl text-teal-400 font-bold")

cols = [
    {"field": "server_id", "headerName": "Server", "width": 95},
    {"field": "gpu", "headerName": "GPU", "width": 140},
    {"field": "cpu", "headerName": "CPU", "flex": 2},
    {"field": "cc", "headerName": "CC", "width": 70},
    {"field": "usd_hr", "headerName": "$/hr", "width": 90, "type": "numericColumn"},
    {"field": "est_sol_hr", "headerName": "est sol/hr", "width": 110, "type": "numericColumn"},
    {"field": "sol_per_usd", "headerName": "sol/$", "width": 100, "type": "numericColumn"},
]

grid = ui.aggrid({
    "columnDefs": cols,
    "rowData": market_rows(),
    "defaultColDef": {"sortable": True, "filter": True, "resizable": True, "flex": 1},
    "pagination": True,
    "paginationPageSize": 100,
}).classes("ag-theme-balham-dark w-full h-[65vh]")

fleet = ui.table(
    title="Fleet",
    columns=[
        {"name": "order_id", "label": "Order", "field": "order_id"},
        {"name": "alias", "label": "Alias", "field": "alias"},
        {"name": "pool", "label": "Pool", "field": "pool"},
        {"name": "health", "label": "Health", "field": "health"},
        {"name": "state", "label": "State", "field": "state"},
        {"name": "shares_hr", "label": "Sh/hr", "field": "shares_hr"},
    ],
    rows=fleet_rows(),
).classes("w-full")


def refresh():
    grid.options["rowData"] = market_rows()
    grid.update()
    fleet.rows = fleet_rows()
    fleet.update()


ui.timer(30, refresh)
ui.run(title="clorqub", port=8081, reload=False, dark=True)
