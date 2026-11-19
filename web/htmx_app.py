#!/usr/bin/env python3
"""FastAPI + HTMX + Pico.css demo: same marketplace table, zero JS to write.

Run:  .venv/bin/pip install fastapi uvicorn
      .venv/bin/python web/htmx_app.py --db ~/.local/share/clorqub/clorqub.sqlite3
      (open http://localhost:8082)
"""
import argparse
import html
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from tui_data import read_dashboard

parser = argparse.ArgumentParser()
parser.add_argument("--db", required=True)
DB = parser.parse_known_args()[0].db
app = FastAPI()

PAGE_SIZE = 100
COLS = [
    ("server_id", "Server"), ("gpu", "GPU"), ("cpu", "CPU"), ("country", "CC"),
    ("hourly_cost_usd", "$/hr"), ("estimated_sols_hr", "est sol/hr"),
    ("estimated_sols_per_usd", "sol/$"),
]

PAGE = """<!doctype html>
<html data-theme="dark">
<head>
<meta charset="utf-8">
<title>clorqub</title>
<link rel="stylesheet"
 href="https://cdn.jsdelivr.net/npm/@picocss/pico@2/css/pico.min.css">
<script src="https://unpkg.com/htmx.org@1.9.12"></script>
<style>
  body { background:#16181c; }
  main { padding:0.5rem 1rem; }
  h3 { color:#2dd4bf; margin-bottom:0.5rem; }
  table { font-size:0.8rem; white-space:nowrap; margin-bottom:0.25rem; }
  th a { color:#2dd4bf; cursor:pointer; text-decoration:none; }
  td.num { text-align:right; font-variant-numeric:tabular-nums; }
  nav.pager { font-size:0.8rem; }
  nav.pager a { color:#2dd4bf; cursor:pointer; margin-right:0.6rem;
                text-decoration:none; }
  nav.pager span.dim { color:#666; margin-right:0.6rem; }
</style>
</head>
<body><main class="container-fluid">
<h3>clorqub — marketplace</h3>
<table>
<thead><tr>%(headers)s</tr></thead>
%(tbody)s
</table>
%(pager)s
</main></body></html>
"""

SORTABLE = {k for k, _ in COLS}


def render_tbody(sort="server_id", page=1):
    rows = [r for r in read_dashboard(DB)["rows"] if r["available"]]
    rows.sort(key=lambda r: (r.get(sort) is None, r.get(sort)))
    total = len(rows)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(max(1, page), pages)
    out = []
    for r in rows[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]:
        esc = lambda v: html.escape(str(v if v is not None else "—"))
        cost = f"{r['hourly_cost_usd']:.3f}" if r["hourly_cost_usd"] else "—"
        out.append(
            f"<tr><td>{r['server_id']}</td><td>{esc(r['gpu'])}</td>"
            f"<td>{esc(r['cpu'])}</td><td>{esc(r['country'])}</td>"
            f"<td class='num'>{cost}</td>"
            f"<td class='num'>{esc(r['estimated_sols_hr'])}</td>"
            f"<td class='num'>{esc(r['estimated_sols_per_usd'])}</td></tr>"
        )
    tbody = (
        f"<tbody id='rows' hx-get='/rows?sort={sort}&page={page}'"
        f" hx-trigger='every 30s' hx-swap='outerHTML'>"
        + "".join(out) + "</tbody>"
    )
    pager = render_pager(sort, page, pages, total)
    return tbody, pager


def page_link(sort, page, label):
    return (f"<a hx-get='/rows?sort={sort}&page={page}' hx-target='#rows'"
            f" hx-swap='outerHTML'>{label}</a>")


def render_pager(sort, page, pages, total):
    parts = [
        page_link(sort, 1, "&laquo;") if page > 1 else "<span class='dim'>&laquo;</span>",
        page_link(sort, page - 1, "&lsaquo;") if page > 1 else "<span class='dim'>&lsaquo;</span>",
        f"<span>page {page} / {pages} &middot; {total} listings</span>",
        page_link(sort, page + 1, "&rsaquo;") if page < pages else "<span class='dim'>&rsaquo;</span>",
        page_link(sort, pages, "&raquo;") if page < pages else "<span class='dim'>&raquo;</span>",
    ]
    return f"<nav id='pager' class='pager' hx-swap-oob='true'>{''.join(parts)}</nav>"


@app.get("/", response_class=HTMLResponse)
def index():
    headers = "".join(
        f"<th><a hx-get='/rows?sort={k}&page=1' hx-target='#rows'"
        f" hx-swap='outerHTML'>{label}</a></th>" for k, label in COLS
    )
    tbody, pager = render_tbody()
    return PAGE % {"headers": headers, "tbody": tbody, "pager": pager}


@app.get("/rows", response_class=HTMLResponse)
def rows(sort: str = "server_id", page: int = 1):
    if sort not in SORTABLE:
        sort = "server_id"
    tbody, pager = render_tbody(sort, page)
    return tbody + pager


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8082)
