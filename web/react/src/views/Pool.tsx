import { useQuery } from '@tanstack/react-query'
import type { ColDef } from 'ag-grid-community'
import type { EChartsOption } from 'echarts'
import { api } from '../api'
import type { PoolWorker } from '../api'
import Chart, { baseGrid } from '../components/Chart'
import { Grid, Panel, Stat } from '../components/ui'
import { ago, num } from '../lib/format'

const POLL = 30_000

const WORKER_COLS: ColDef<PoolWorker>[] = [
  { field: 'alias', headerName: 'Worker', flex: 1 },
  { field: 'sol', headerName: 'sol', width: 110, type: 'numericColumn', valueFormatter: (p) => num(p.value, 1) },
  { field: 'its', headerName: 'it/s', width: 100, type: 'numericColumn', valueFormatter: (p) => num(p.value, 1) },
  { field: 'trainer', headerName: 'Trainer', width: 110 },
  { field: 'cuda', headerName: 'CUDA', width: 90, type: 'numericColumn', valueFormatter: (p) => num(p.value, 1) },
  { field: 'avx512', headerName: 'AVX512', width: 90, type: 'numericColumn', valueFormatter: (p) => num(p.value, 1) },
  { field: 'avx2', headerName: 'AVX2', width: 90, type: 'numericColumn', valueFormatter: (p) => num(p.value, 1) },
  { field: 'last_seen', headerName: 'Last seen', width: 120, valueFormatter: (p) => ago(p.value) },
]

export default function Pool() {
  const series = useQuery({ queryKey: ['poolSeries'], queryFn: api.poolSeries, refetchInterval: POLL })
  const workers = useQuery({ queryKey: ['poolWorkers'], queryFn: api.poolWorkers, refetchInterval: POLL })
  const pts = series.data?.points ?? []
  const last = pts[pts.length - 1]

  const solOption: EChartsOption = {
    ...baseGrid(),
    series: [{
      name: 'pool sol/hr', type: 'line', showSymbol: false, smooth: true,
      data: pts.map((p) => [p.source_ts, p.pool_sol_per_hour]),
      areaStyle: { opacity: 0.15 },
    }],
  }
  const econOption: EChartsOption = {
    ...baseGrid(),
    series: [
      { name: 'est QUs', type: 'line', showSymbol: false, data: pts.map((p) => [p.source_ts, p.est_qus]) },
      { name: 'counted', type: 'line', showSymbol: false, data: pts.map((p) => [p.source_ts, p.counted]) },
      { name: 'solutions', type: 'line', showSymbol: false, data: pts.map((p) => [p.source_ts, p.solutions]) },
    ],
  }
  const priceOption: EChartsOption = {
    ...baseGrid(),
    series: [
      { name: '$/counted', type: 'line', showSymbol: false, data: pts.map((p) => [p.source_ts, p.usd_per_counted]) },
      {
        name: 'QUBIC $', type: 'line', showSymbol: false, yAxisIndex: 1,
        data: pts.map((p) => [p.source_ts, p.qubic_price_usd]),
      },
    ],
    yAxis: [
      { ...baseGrid().yAxis },
      { ...baseGrid().yAxis, position: 'right', splitLine: { show: false } },
    ],
  }
  // v = the runner's per-share valuation: P (pool $/counted) x w_bar
  // (counted credited per submitted share) -- the same number that
  // drives every fleet decision, decomposed into its two inputs
  const wPts = pts.map((p) => [p.source_ts,
    p.shares ? (p.counted ?? 0) / p.shares : null])
  const vPts = pts.map((p, i) => [p.source_ts,
    p.usd_per_counted != null && wPts[i][1] != null
      ? (wPts[i][1] as number) * p.usd_per_counted : null])
  const vOption: EChartsOption = {
    ...baseGrid(),
    series: [
      { name: 'v $/share', type: 'line', showSymbol: false, smooth: true,
        data: vPts, areaStyle: { opacity: 0.15 } },
      { name: 'w̄ counted/share', type: 'line', showSymbol: false,
        yAxisIndex: 1, lineStyle: { type: 'dashed' }, data: wPts },
    ],
    yAxis: [
      { ...baseGrid().yAxis },
      { ...baseGrid().yAxis, position: 'right', splitLine: { show: false } },
    ],
  }
  const lastV = vPts.length ? (vPts[vPts.length - 1][1] as number | null) : null
  const lastW = wPts.length ? (wPts[wPts.length - 1][1] as number | null) : null

  return (
    <div className="space-y-3">
      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
        <Stat label="Pool sol/hr" value={num(last?.pool_sol_per_hour, 0)} tone="good" />
        <Stat label="Epoch" value={num(last?.epoch)} />
        <Stat label="Est QUs" value={num(last?.est_qus, 1)} />
        <Stat label="$/counted" value={num(last?.usd_per_counted, 5)} />
        <Stat label="QUBIC $" value={num(last?.qubic_price_usd, 8)} />
        <Stat label="v $/share" value={num(lastV, 4)}
          sub={lastW != null ? `w̄ ${num(lastW, 2)} counted/sh` : undefined} />
      </div>
      <div className="grid grid-cols-1 gap-3 xl:grid-cols-3">
        <Panel title="Pool sol/hr"><Chart option={solOption} height={220} /></Panel>
        <Panel title="Earnings accounting"><Chart option={econOption} height={220} /></Panel>
        <Panel title="Prices"><Chart option={priceOption} height={220} /></Panel>
        <Panel title="Share value (v)"><Chart option={vOption} height={220} /></Panel>
      </div>
      <Panel title={`Pool workers (${workers.data?.points.length ?? 0})`}>
        <Grid rows={workers.data?.points ?? []} colDefs={WORKER_COLS} height={320} />
      </Panel>
    </div>
  )
}
