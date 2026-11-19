import {
  AllCommunityModule,
  ModuleRegistry,
  colorSchemeDark,
  themeBalham,
} from 'ag-grid-community'
import type { ColDef, GridApi } from 'ag-grid-community'
import { AgGridReact } from 'ag-grid-react'
import { useCallback, useRef, useState } from 'react'
import type { ReactNode } from 'react'

ModuleRegistry.registerModules([AllCommunityModule])
export const gridTheme = themeBalham.withPart(colorSchemeDark).withParams({
  backgroundColor: '#1a1d22',
  headerBackgroundColor: '#16181c',
  accentColor: '#2dd4bf',
  fontSize: 12,
})

export function Panel({ title, children, right, className = '' }: {
  title?: string
  children: ReactNode
  right?: ReactNode
  className?: string
}) {
  return (
    <section className={`rounded-lg border border-gray-800 bg-[#1a1d22] ${className}`}>
      {(title || right) && (
        <header className="flex items-center justify-between border-b border-gray-800 px-3 py-1.5">
          <h3 className="text-sm font-semibold text-teal-300">{title}</h3>
          {right}
        </header>
      )}
      <div className="p-3">{children}</div>
    </section>
  )
}

export function Stat({ label, value, sub, tone = 'default' }: {
  label: string
  value: string
  sub?: string
  tone?: 'default' | 'good' | 'warn' | 'bad'
}) {
  const tones = {
    default: 'text-gray-100',
    good: 'text-teal-300',
    warn: 'text-amber-400',
    bad: 'text-red-400',
  }
  return (
    <div className="rounded-lg border border-gray-800 bg-[#1a1d22] px-3 py-2">
      <div className="text-[11px] uppercase tracking-wide text-gray-500">{label}</div>
      <div className={`text-xl font-semibold tabular-nums ${tones[tone]}`}>{value}</div>
      {sub && <div className="text-[11px] text-gray-500">{sub}</div>}
    </div>
  )
}

export function Badge({ children, tone = 'gray' }: {
  children: ReactNode
  tone?: 'gray' | 'teal' | 'amber' | 'red' | 'indigo'
}) {
  const tones = {
    gray: 'bg-gray-700/40 text-gray-300',
    teal: 'bg-teal-500/15 text-teal-300',
    amber: 'bg-amber-500/15 text-amber-400',
    red: 'bg-red-500/15 text-red-400',
    indigo: 'bg-indigo-500/15 text-indigo-300',
  }
  return (
    <span className={`inline-block rounded px-1.5 py-0.5 text-[11px] ${tones[tone]}`}>
      {children}
    </span>
  )
}

export function Grid<T>({ rows, colDefs, height = 480, pageSize = 100, onRowClicked }: {
  rows: T[]
  colDefs: ColDef<T>[]
  height?: number | string
  pageSize?: number
  onRowClicked?: (row: T) => void
}) {
  const apiRef = useRef<GridApi<T> | null>(null)
  const [copied, setCopied] = useState(false)
  const [filters, setFilters] = useState(false)
  const [search, setSearch] = useState('')

  const copyTable = useCallback(async () => {
    const api = apiRef.current
    if (!api) return
    const cols = api.getAllDisplayedColumns()
    const lines = [
      cols.map((c) => c.getColDef().headerName ?? c.getColId()).join('\t'),
    ]
    api.forEachNodeAfterFilterAndSort((node) => {
      if (!node.data) return
      lines.push(
        cols
          .map((c) =>
            String(
              api.getCellValue({ rowNode: node, colKey: c, useFormatter: true }) ?? '',
            ).replace(/[\t\r\n]+/g, ' '),
          )
          .join('\t'),
      )
    })
    const tsv = lines.join('\n')
    try {
      await navigator.clipboard.writeText(tsv)
    } catch {
      const ta = document.createElement('textarea')
      ta.value = tsv
      document.body.appendChild(ta)
      ta.select()
      document.execCommand('copy')
      ta.remove()
    }
    setCopied(true)
    setTimeout(() => setCopied(false), 1500)
  }, [])

  return (
    <div className="flex w-full flex-col gap-1">
      <div className="flex items-center gap-1">
        <input
          type="search"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="search…"
          className="w-40 rounded border border-gray-700 bg-[#0d0f12] px-2 py-0.5 text-[11px] text-gray-200 outline-none focus:border-teal-600"
        />
        <span className="flex-1" />
        <button
          type="button"
          onClick={() => {
            setFilters((f) => {
              // a hidden active filter silently slices rows -- clear
              // the model when the row is toggled off
              if (f) apiRef.current?.setFilterModel(null)
              return !f
            })
          }}
          className={`rounded border px-2 py-0.5 text-[11px] ${
            filters
              ? 'border-teal-600 text-teal-300'
              : 'border-gray-700 text-gray-400 hover:border-teal-600 hover:text-teal-300'
          }`}
        >
          {filters ? 'Filters ✓' : 'Filters'}
        </button>
        <button
          type="button"
          onClick={copyTable}
          className="rounded border border-gray-700 px-2 py-0.5 text-[11px] text-gray-400 hover:border-teal-600 hover:text-teal-300"
        >
          {copied ? 'Copied!' : 'Copy table'}
        </button>
      </div>
      <div style={{ height }}>
        <AgGridReact<T>
          theme={gridTheme}
          rowData={rows}
          columnDefs={colDefs}
          defaultColDef={{
            sortable: true,
            resizable: true,
            filter: true,
            floatingFilter: filters,
          }}
          autoSizeStrategy={{
            type: 'fitCellContents',
            scaleUpToFitGridWidth: true,
          }}
          suppressDragLeaveHidesColumns
          pagination
          paginationPageSize={pageSize}
          rowClass="cursor-pointer"
          enableCellTextSelection
          ensureDomOrder
          quickFilterText={search}
          onGridReady={(e) => {
            apiRef.current = e.api
          }}
          onRowClicked={onRowClicked ? (e) => e.data && onRowClicked(e.data) : undefined}
        />
      </div>
    </div>
  )
}

export function pinColumn<T>(pinSet: Set<number>,
                             onToggle: (serverId: number, on: boolean) => void): ColDef<T> {
  return {
    colId: 'pinned',
    headerName: 'pin', width: 50, sortable: false,
    suppressMovable: true, resizable: false,
    filter: 'agTextColumnFilter',
    valueGetter: (p) => {
      const sid = (p.data as { server_id?: number } | undefined)?.server_id
      return sid != null && pinSet.has(sid) ? '★ pinned' : ''
    },
    cellRenderer: (p: { data?: T }) => {
      const sid = (p.data as { server_id?: number } | undefined)?.server_id
      const on = sid != null && pinSet.has(sid)
      return (
        <button
          title={on
            ? 'pinned — auditions first on the next probe; click to unpin'
            : 'pin for the next probe burst'}
          onClick={(e) => {
            // don't let a pin toggle bubble into row-click handlers
            // (Marketplace opens the server drawer on row click)
            e.stopPropagation()
            if (sid != null) onToggle(sid, !on)
          }}
          className={on ? 'text-teal-300' : 'text-gray-400 hover:text-teal-400'}>
          {on ? '★' : '☆'}
        </button>
      )
    },
  }
}

export function KV({ items }: { items: [string, ReactNode][] }) {
  return (
    <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
      {items.map(([k, v]) => (
        <div key={k} className="contents">
          <dt className="text-gray-500">{k}</dt>
          <dd className="text-gray-200">{v ?? '—'}</dd>
        </div>
      ))}
    </dl>
  )
}
