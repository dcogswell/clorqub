import { useQuery, useQueryClient } from '@tanstack/react-query'
import type { ColDef } from 'ag-grid-community'
import { useMemo, useState } from 'react'
import { api } from '../api'
import type { MarketRow } from '../api'
import ServerDrawer from '../components/ServerDrawer'
import { Grid, pinColumn } from '../components/ui'
import { num } from '../lib/format'

const POLL = 30_000

const COLS: ColDef<MarketRow>[] = [
  { field: 'server_id', headerName: 'Server', width: 95 },
  { field: 'gpu', headerName: 'GPU', width: 140, valueFormatter: (p) => p.value ?? '—' },
  { field: 'cpu', headerName: 'CPU', flex: 2, minWidth: 220 },
  { field: 'country', headerName: 'CC', width: 75 },
  {
    field: 'hourly_cost_usd', headerName: '$/hr', width: 95, type: 'numericColumn',
    valueFormatter: (p) => (p.value == null ? '—' : p.value.toFixed(3)),
  },
  {
    field: 'mode', headerName: 'Mode', width: 105,
    valueFormatter: (p) =>
      p.value === 'spot'
      && typeof p.data?.spot_discount === 'number'
      && p.data.spot_discount > 0
        ? `spot -${Math.round(p.data.spot_discount * 100)}%`
        : '—',
  },
  {
    field: 'est_its', headerName: 'est it/s', width: 105, type: 'numericColumn',
    valueFormatter: (p) => num(p.value, 0),
    cellStyle: { color: '#2dd4bf' },
  },
  {
    field: 'est_its_per_usd', headerName: 'it/$', width: 95, type: 'numericColumn',
    valueFormatter: (p) => num(p.value, 0),
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
  { field: 'ineligible_reason', headerName: 'Note', flex: 1 },
]

export default function Candidates() {
  const qc = useQueryClient()
  const market = useQuery({ queryKey: ['market'], queryFn: api.market, refetchInterval: POLL })
  const cands = useQuery({ queryKey: ['experiment', 'candidates'], queryFn: api.candidates, refetchInterval: POLL })
  const pins = useQuery({ queryKey: ['probePins'], queryFn: api.probePins, refetchInterval: POLL })
  const pinSet = useMemo(
    () => new Set((pins.data?.pins ?? []).map((p) => p.server_id)),
    [pins.data])
  const upStatus = useMemo(
    () => new Map(
      (cands.data?.candidate_values ?? [])
        .map((c) => [c.server_id, c.upgrade_status] as const)),
    [cands.data])
  const [sel, setSel] = useState<MarketRow | null>(null)
  const [rentedOnly, setRentedOnly] = useState(false)
  const rows = (market.data ?? [])
    .filter((r) => r.eligible && r.available)
    .filter((r) => !rentedOnly || (r.prior_rentals ?? 0) > 0)
    .map((r) => ({ ...r, upgrade_status: upStatus.get(r.server_id) ?? null }))
  const unlistedPins = pinSet.size
    - rows.reduce((n, r) => n + (pinSet.has(r.server_id) ? 1 : 0), 0)
  const cols = useMemo<ColDef<MarketRow>[]>(() => [
    pinColumn<MarketRow>(pinSet, (sid, on) =>
      api.setProbePin(sid, on)
        .then(() => qc.invalidateQueries({ queryKey: ['probePins'] }))),
    ...COLS], [pinSet, qc])
  return (
    <div>
      <div className="mb-2 flex items-center gap-3 text-sm text-gray-400">
        <span>{rows.length} ranked candidates</span>
        <label className="flex cursor-pointer items-center gap-1.5">
          <input type="checkbox" checked={rentedOnly}
            onChange={(e) => setRentedOnly(e.target.checked)}
            className="accent-teal-400" />
          rented before
        </label>
        {pinSet.size > 0 && (
          <span className="text-gray-600">
            ★{pinSet.size} pinned{unlistedPins ? ` (${unlistedPins} not in table)` : ''}
          </span>
        )}
        <span className="text-gray-600">click a row for history + rent</span>
      </div>
      <Grid rows={rows} colDefs={cols} height="calc(100vh - 170px)"
        onRowClicked={(r) => setSel(r)} />
      {sel != null && (
        <ServerDrawer key={sel.server_id} serverId={sel.server_id} row={sel} onClose={() => setSel(null)} />
      )}
    </div>
  )
}
