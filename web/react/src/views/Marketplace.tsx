import { useQuery, useQueryClient } from '@tanstack/react-query'
import type { ColDef } from 'ag-grid-community'
import { useMemo, useState } from 'react'
import { api } from '../api'
import type { MarketRow } from '../api'
import ServerDrawer from '../components/ServerDrawer'
import { Badge, Grid, pinColumn } from '../components/ui'


const POLL = 30_000

function scoreCell(p: { value: number | null }) {
  if (p.value == null) return '—'
  const v = p.value
  const cls = v > 800 ? 'text-teal-300 font-semibold'
    : v > 300 ? 'text-teal-500' : 'text-gray-400'
  return <span className={cls}>{v.toFixed(0)}</span>
}

const COLS: ColDef<MarketRow>[] = [
  { field: 'server_id', headerName: 'Server', width: 90 },
  {
    field: 'gpu', headerName: 'GPU', width: 140,
    valueFormatter: (p) => p.value ?? '—',
    cellStyle: (p) => (p.value ? { color: '#e5e7eb' } : { color: '#4b5563' }),
  },
  { field: 'cpu', headerName: 'CPU', flex: 2, minWidth: 220 },
  { field: 'country', headerName: 'CC', width: 75 },
  { field: 'cpu_isa', headerName: 'ISA', width: 130 },
  { field: 'threads_listed', headerName: 'Threads', width: 95 },
  {
    field: 'hourly_cost_usd', headerName: '$/hr', width: 95, type: 'numericColumn',
    valueFormatter: (p) => (p.value == null ? '—' : p.value.toFixed(3)),
  },
  {
    field: 'est_its', headerName: 'est it/s', width: 105, type: 'numericColumn',
    cellRenderer: scoreCell,
  },
  {
    field: 'est_its_per_usd', headerName: 'it/$', width: 105, type: 'numericColumn',
    cellRenderer: scoreCell,
  },
  {
    field: 'reliability', headerName: 'rel', width: 85, type: 'numericColumn',
    valueFormatter: (p) => (p.value == null ? '—' : p.value.toFixed(2)),
  },
  {
    field: 'prior_rentals', headerName: 'rented', width: 85, type: 'numericColumn',
    valueFormatter: (p) => (p.value ? `${p.value}×` : ''),
  },
  {
    field: 'upgrade_status', headerName: 'Upgrade', width: 100,
    cellRenderer: (p: { value: string | null }) =>
      p.value === 'tried' ? <span className="text-amber-400/80">{p.value}</span> : p.value,
  },
  {
    field: 'ineligible_reason', headerName: 'note', flex: 1,
    cellRenderer: (p: { value: string | null; data?: MarketRow }) =>
      p.data?.available === false
        ? <span className="text-gray-600">taken</span>
        : p.value ? <span className="text-amber-400/80">{p.value}</span> : '',
  },
]

export default function Marketplace() {
  const qc = useQueryClient()
  const market = useQuery({ queryKey: ['market'], queryFn: api.market, refetchInterval: POLL })
  const cands = useQuery({ queryKey: ['experiment', 'candidates'], queryFn: api.candidates, refetchInterval: POLL })
  const pins = useQuery({ queryKey: ['probePins'], queryFn: api.probePins, refetchInterval: POLL })
  const pinSet = useMemo(
    () => new Set((pins.data?.pins ?? []).map((p) => p.server_id)),
    [pins.data])
  const slate = useMemo(
    () => new Map(
      (cands.data?.candidate_values ?? [])
        .map((c, i) => [c.server_id, { pick: i + 1, upgrade: c.upgrade_status }] as const)),
    [cands.data])
  const [sel, setSel] = useState<MarketRow | null>(null)
  const [hideTaken, setHideTaken] = useState(true)
  const [eligibleOnly, setEligibleOnly] = useState(false)
  const [rentedOnly, setRentedOnly] = useState(false)
  const [blOnly, setBlOnly] = useState(false)

  const rows = (market.data ?? [])
    .filter((r) => !hideTaken || r.available)
    .filter((r) => !eligibleOnly || r.eligible)
    .filter((r) => !rentedOnly || (r.prior_rentals ?? 0) > 0)
    .filter((r) => !blOnly || r.blacklist != null)
    .map((r) => ({ ...r, upgrade_status: slate.get(r.server_id)?.upgrade ?? null }))
    .sort((a, b) =>
      (slate.get(a.server_id)?.pick ?? 1e9) - (slate.get(b.server_id)?.pick ?? 1e9))
  const unlistedPins = pinSet.size
    - rows.reduce((n, r) => n + (pinSet.has(r.server_id) ? 1 : 0), 0)
  const cols = useMemo<ColDef<MarketRow>[]>(() => [
    pinColumn<MarketRow>(pinSet, (sid, on) =>
      api.setProbePin(sid, on)
        .then(() => qc.invalidateQueries({ queryKey: ['probePins'] }))),
    {
      headerName: 'BL', width: 50, sortable: false, filter: false,
      suppressMovable: true, resizable: false,
      cellRenderer: (p: { data?: MarketRow }) => {
        const bl = p.data?.blacklist
        if (!bl) return ''
        return (
          <span title={`blacklisted (${bl.source}): ${bl.reason}`}>
            <Badge tone="red">BL</Badge>
          </span>
        )
      },
    },
    ...COLS], [pinSet, qc])

  return (
    <div>
      <div className="mb-2 flex items-center gap-3 text-sm text-gray-400">
        <label className="flex cursor-pointer items-center gap-1.5">
          <input type="checkbox" checked={hideTaken} onChange={(e) => setHideTaken(e.target.checked)}
            className="accent-teal-400" />
          available only ({rows.length})
        </label>
        <label className="flex cursor-pointer items-center gap-1.5">
          <input type="checkbox" checked={eligibleOnly}
            onChange={(e) => setEligibleOnly(e.target.checked)}
            className="accent-teal-400" />
          eligible only
        </label>
        <label className="flex cursor-pointer items-center gap-1.5">
          <input type="checkbox" checked={rentedOnly}
            onChange={(e) => setRentedOnly(e.target.checked)}
            className="accent-teal-400" />
          rented before
        </label>
        <label className="flex cursor-pointer items-center gap-1.5">
          <input type="checkbox" checked={blOnly}
            onChange={(e) => setBlOnly(e.target.checked)}
            className="accent-red-400" />
          blacklisted
        </label>
        <span className="text-gray-600">
          {pinSet.size > 0
            ? `★${pinSet.size} pinned${unlistedPins ? ` (${unlistedPins} not in table)` : ''} · `
            : ''}
          click a row for server history
        </span>
      </div>
      <Grid rows={rows} colDefs={cols} height="calc(100vh - 170px)"
        onRowClicked={(r) => setSel(r)} />
      {sel != null && (
        <ServerDrawer key={sel.server_id} serverId={sel.server_id} row={sel} onClose={() => setSel(null)} />
      )}
    </div>
  )
}
