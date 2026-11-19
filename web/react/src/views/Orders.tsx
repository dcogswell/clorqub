import { useQuery } from '@tanstack/react-query'
import type { ColDef } from 'ag-grid-community'
import { api } from '../api'
import type { Order } from '../api'
import { Badge, Grid } from '../components/ui'
import { dt, num } from '../lib/format'

const POLL = 30_000

const COLS: ColDef<Order>[] = [
  { field: 'order_id', headerName: 'Order', width: 100 },
  { field: 'server_id', headerName: 'Server', width: 95 },
  {
    field: 'state', headerName: 'State', width: 95,
    cellRenderer: (p: { value: string }) =>
      <Badge tone={p.value === 'active' ? 'teal' : 'gray'}>{p.value}</Badge>,
  },
  {
    field: 'gpus', headerName: 'GPUs', width: 150,
    valueFormatter: (p) => (p.value ?? []).join(', ') || '—',
  },
  { field: 'cpu', headerName: 'CPU', flex: 2, minWidth: 200 },
  {
    field: 'price_day_native', headerName: '$/day', width: 90, type: 'numericColumn',
    valueFormatter: (p) => num(p.value, 2),
  },
  {
    field: 'spend_reported_native', headerName: 'spent', width: 90, type: 'numericColumn',
    valueFormatter: (p) => num(p.value, 3),
  },
  {
    field: 'shares_hr', headerName: 'sh/hr', width: 95, type: 'numericColumn',
    valueFormatter: (p) => num(p.value, 0),
    cellStyle: (p) => (p.value != null && p.value > 0 ? { color: '#2dd4bf' } : undefined),
  },
  {
    field: 'shares_per_cent', headerName: 'sh/¢', width: 90, type: 'numericColumn',
    valueFormatter: (p) => num(p.value, 1),
  },
  {
    field: 'online_reported', headerName: 'online', width: 85,
    cellRenderer: (p: { value: boolean | null }) =>
      p.value == null ? '' : p.value ? '✓' : <span className="text-red-400">✗</span>,
  },
  { field: 'first_seen', headerName: 'First seen', width: 165, valueFormatter: (p) => dt(p.value) },
  { field: 'last_seen', headerName: 'Last seen', width: 165, valueFormatter: (p) => dt(p.value) },
]

export default function Orders() {
  const orders = useQuery({ queryKey: ['orders'], queryFn: api.orders, refetchInterval: POLL })
  return <Grid rows={orders.data ?? []} colDefs={COLS} height="calc(100vh - 140px)" />
}
