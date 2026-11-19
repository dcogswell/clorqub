import { useQuery, useQueryClient } from '@tanstack/react-query'
import type { ColDef } from 'ag-grid-community'
import type { EChartsOption } from 'echarts'
import { useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api'
import type { Rig } from '../api'
import Chart, { baseGrid } from '../components/Chart'
import { Modal } from '../components/RentModal'
import { Badge, Grid, KV, Panel } from '../components/ui'
import { ago, dt, duration, int, num, usd } from '../lib/format'

const POLL = 30_000

const STATE_TONE: Record<string, 'teal' | 'amber' | 'red' | 'gray' | 'indigo'> = {
  ready: 'teal', provisioning: 'amber', cancelled: 'gray',
  missing: 'red', provision_failed: 'red', observe_only: 'indigo',
}

function gpuLabel(r: Rig): string {
  const names = (
    r.status?.hardware?.gpus
    ?? (r.order_gpus as string[] | null)
    ?? []
  )
    .map((s) => String(s)
      // observed hardware names are verbose: strip the "GPU 0:"
      // prefix, "(UUID: …)" suffix, and "NVIDIA GeForce" branding
      .replace(/^GPU\s*\d+\s*:\s*/i, '')
      .replace(/\s*\(UUID:[^)]*\)/gi, '')
      .replace(/^(NVIDIA\s+)?(GeForce\s+)?/i, '')
      .trim())
    .filter(Boolean)
  const counts = new Map<string, number>()
  for (const n of names) counts.set(n, (counts.get(n) ?? 0) + 1)
  return [...counts.entries()]
    .map(([n, c]) => (c > 1 ? `${c}× ${n}` : n))
    .join(', ')
}

const COLS: ColDef<Rig>[] = [
  {
    field: 'alias', headerName: 'Rig', flex: 1.2, minWidth: 140,
    cellRenderer: (p: { value: string; data?: Rig }) => (
      <span>
        {p.value}
        {p.data?.control === 'experiment' && <Badge tone="indigo">exp</Badge>}
      </span>
    ),
  },
  {
    headerName: 'GPU', width: 140,
    valueGetter: (p) => (p.data ? gpuLabel(p.data) : ''),
  },
  {
    field: 'provision_state', headerName: 'State', width: 120,
    cellRenderer: (p: { value: string }) =>
      <Badge tone={STATE_TONE[p.value] ?? 'gray'}>{p.value}</Badge>,
  },
  {
    field: 'mode', headerName: 'Mode', width: 75,
    valueFormatter: (p) => (p.value === 'spot' ? 'spot' : '—'),
  },
  {
    field: 'status.shares.submitted', headerName: 'shares', width: 95,
    type: 'numericColumn', valueFormatter: (p) => int(p.value),
  },
  {
    field: 'status.its.gpu', headerName: 'GPU it/s', width: 95,
    type: 'numericColumn', valueFormatter: (p) => num(p.value, 1),
  },
  {
    field: 'status.its.cpu', headerName: 'CPU it/s', width: 95,
    type: 'numericColumn', valueFormatter: (p) => num(p.value, 1),
  },
  {
    field: 'rate_hr_usd', headerName: 'burn $/hr', width: 105,
    type: 'numericColumn',
    valueFormatter: (p) => (p.value == null ? '—' : `$${Number(p.value).toFixed(3)}`),
  },
  {
    field: 'spend_usd', headerName: 'spent', width: 90, type: 'numericColumn',
    valueFormatter: (p) => (p.value == null ? '—' : `$${Number(p.value).toFixed(2)}`),
  },
  {
    field: 'created_at', headerName: 'Uptime', width: 110,
    valueFormatter: (p) => duration(p.value, p.data?.up_until as string | undefined),
  },
  {
    field: 'deadline_at', headerName: 'Deadline', width: 160,
    valueFormatter: (p) => dt(p.value),
  },
  {
    field: 'observed_at', headerName: 'Last poll', width: 110,
    valueFormatter: (p) => ago(p.value),
    cellStyle: (p) => {
      const s = p.value ? (Date.now() - new Date(p.value).getTime()) / 1000 : Infinity
      return s > 180 ? { color: '#f59e0b' } : undefined
    },
  },
  {
    field: 'last_share_at', headerName: 'Last share', width: 105,
    valueFormatter: (p) => ago(p.value),
  },
  {
    field: 'error_kind', headerName: 'Error', flex: 1,
    cellRenderer: (p: { value: string | null }) =>
      p.value ? <span className="text-red-400">{p.value}</span> : '',
  },
]

const CANCELLABLE = new Set(['ready', 'provisioning', 'enrolling', 'enrolled'])

function CancelModal({ rig, onClose }: { rig: Rig; onClose: () => void }) {
  const [typed, setTyped] = useState('')
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<{ rc: number; lines: string[] } | null>(null)
  const [error, setError] = useState('')
  const qc = useQueryClient()
  const match = typed.trim().replace(/^cancel\s+/i, '')
    === String(rig.order_id)

  function run() {
    setBusy(true)
    api.cancelOrder(rig.order_id)
      .then((res) => {
        setResult(res)
        qc.invalidateQueries({ queryKey: ['fleet'] })
        qc.invalidateQueries({ queryKey: ['orders'] })
        qc.invalidateQueries({ queryKey: ['summary'] })
      })
      .catch((e) => setError(String(e.message ?? e)))
      .finally(() => setBusy(false))
  }

  return (
    <Modal title={`cancel order ${rig.order_id}`} onClose={busy ? () => {} : onClose} width={440}>
      {result ? (
        <div className="space-y-3 text-sm">
          <p className={result.rc === 0 ? 'font-semibold text-teal-300' : 'font-semibold text-amber-400'}>
            {result.rc === 0 ? `order ${rig.order_id} cancelled` : `cancel ended rc=${result.rc}`}
          </p>
          <pre className="max-h-48 overflow-auto rounded bg-[#0d0f12] p-2 font-mono text-[11px] text-gray-400">
            {result.lines.join('\n')}
          </pre>
          <div className="flex justify-end">
            <button onClick={onClose}
              className="rounded bg-teal-600 px-3 py-1.5 text-sm font-semibold text-white">Close</button>
          </div>
        </div>
      ) : (
        <div className="space-y-3 text-sm">
          <p className="text-gray-300">
            Cancelling order <b>#{rig.order_id}</b> ({rig.alias}) sends ONE cancel_order POST,
            no retries. Billing stops; the rig is released.
          </p>
          <p className="text-xs text-gray-500">
            type <b className="text-amber-400">cancel {rig.order_id}</b> to confirm:
          </p>
          <input
            autoFocus value={typed} onChange={(e) => setTyped(e.target.value)}
            placeholder={`cancel ${rig.order_id}`}
            className="w-full rounded border border-gray-700 bg-[#0d0f12] px-2 py-1.5 font-mono text-sm text-gray-200 outline-none focus:border-red-500" />
          {error && <p className="text-sm text-red-400">{error}</p>}
          <div className="flex justify-end gap-2">
            <button onClick={onClose} disabled={busy}
              className="rounded border border-gray-700 px-3 py-1.5 text-sm text-gray-300">
              Back
            </button>
            <button onClick={run} disabled={!match || busy}
              className="rounded bg-red-600 px-3 py-1.5 text-sm font-semibold text-white disabled:opacity-40">
              Cancel order #{rig.order_id}
            </button>
          </div>
        </div>
      )}
    </Modal>
  )
}

function RigDetails({ rig }: { rig: Rig }) {
  const [cancelOpen, setCancelOpen] = useState(false)
  const hw = rig.status?.hardware
  return (
    <>
      <Panel title="Rig">
        <KV items={[
          ['order', `#${rig.order_id}`],
          ['server', rig.server_id],
          ['pool', rig.status?.pool ?? rig.pool],
          ['cpu', hw?.cpu_model],
          ['threads', hw?.nproc],
          ['gpus', (hw?.gpus ?? []).join(', ').slice(0, 80)],
          ['avx', hw ? `${hw.avx512f ? 'AVX512' : ''}${hw.avx2 ? ' AVX2' : ''}` : '—'],
          ['package', rig.status?.package?.version],
          ['miner', rig.status?.miner ? (rig.status.miner.running ? 'running' : 'stopped') : '—'],
          ['epoch', rig.status?.shares?.epoch],
          ['cost cap', `${usd(Number(rig.max_cost_usd), 2)} / ${usd(Number(rig.quoted_cost_usd), 2)} quoted`],
          ['burn', usd(rig.rate_hr_usd, 3) + '/hr'],
          ['spent', usd(rig.spend_usd, 2)],
          ['ssh', rig.ssh_host ? `${String(rig.ssh_host)}:${String(rig.ssh_port ?? '')}` : '—'],
        ]} />
        {CANCELLABLE.has(rig.provision_state) && (
          <div className="mt-3">
            <button onClick={() => setCancelOpen(true)}
              className="rounded border border-red-800 px-3 py-1.5 text-sm font-semibold text-red-400 hover:bg-red-500/10">
              Cancel order
            </button>
          </div>
        )}
      </Panel>
      {cancelOpen && <CancelModal rig={rig} onClose={() => setCancelOpen(false)} />}
    </>
  )
}

function RigCharts({ rig }: { rig: Rig }) {
  const series = useQuery({
    queryKey: ['rigSeries', rig.order_id],
    queryFn: () => api.rigSeries(rig.order_id),
    refetchInterval: POLL,
  })
  const spend = useQuery({
    queryKey: ['rigSpend', rig.order_id],
    queryFn: () => api.rigSpend(rig.order_id),
    refetchInterval: POLL,
  })
  const pts = useMemo(() => (series.data?.points ?? []).filter((p) => !p.error), [series.data])

  const spendOption: EChartsOption = {
    ...baseGrid(),
    series: [
      {
        name: 'spent $', type: 'line', showSymbol: false, smooth: true, step: 'end',
        data: (spend.data?.points ?? []).map((p) => [p.ts, p.spend]),
        areaStyle: { opacity: 0.15 },
      },
    ],
  }

  const sharesOption: EChartsOption = {
    ...baseGrid(),
    series: [
      {
        name: 'submitted', type: 'line', showSymbol: false, smooth: true,
        data: pts.map((p) => [p.ts, p.submitted]), areaStyle: { opacity: 0.15 },
      },
      {
        name: 'invalid', type: 'line', showSymbol: false,
        data: pts.map((p) => [p.ts, p.invalid]),
      },
    ],
  }
  const itsOption: EChartsOption = {
    ...baseGrid(),
    series: [
      {
        name: 'GPU it/s', type: 'line', showSymbol: false, smooth: true,
        data: pts.map((p) => [p.ts, p.gpu_its]),
      },
      {
        name: 'CPU it/s', type: 'line', showSymbol: false, smooth: true,
        data: pts.map((p) => [p.ts, p.cpu_its]),
      },
    ],
  }

  return (
    <div className="space-y-3">
      <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
        <Panel title="Shares (cumulative)">
          <Chart option={sharesOption} height={240} />
        </Panel>
        <Panel title="Iterations/s">
          <Chart option={itsOption} height={240} />
        </Panel>
      </div>
      <Panel title="Spend over time (reported)">
        <Chart option={spendOption} height={200} />
      </Panel>
    </div>
  )
}

function RigLog({ rig }: { rig: Rig }) {
  const [logSlice, setLogSlice] = useState(50) // >0 last N, <0 first N
  const headMode = logSlice < 0
  const log = useQuery({
    // head mode reads the archive's oldest bytes (startup/config
    // output), which the tail-capped default view drops once the
    // log outgrows 200KB; the head never changes, so no polling
    queryKey: ['rigLog', rig.order_id, headMode],
    queryFn: () => api.rigLog(rig.order_id, rig.pool, headMode),
    refetchInterval: headMode ? false : POLL,
  })
  const logRef = useRef<HTMLPreElement>(null)
  const jumpedToEnd = useRef<number | null>(null)
  const logText = log.data?.text ?? ''
  const shownLog = useMemo(() => {
    if (!logText || logSlice === 0) return logText
    const lines = logText.split('\n')
    return (logSlice > 0
      ? lines.slice(-logSlice)
      : lines.slice(0, -logSlice)).join('\n')
  }, [logText, logSlice])
  useEffect(() => {
    // log appends at the bottom: land on the newest lines the first
    // time a rig's archive arrives. Once per rig in full mode -- the
    // 30s refresh must never yank an open scroll position; tail mode
    // pins to the end like tail -f, head mode pins to the top
    const el = logRef.current
    if (!el || !log.data?.text) return
    if (logSlice < 0) {
      el.scrollTop = 0
    } else if (logSlice > 0 || jumpedToEnd.current !== rig.order_id) {
      jumpedToEnd.current = rig.order_id
      el.scrollTop = el.scrollHeight
    }
  }, [log.data, rig.order_id, logSlice])

  return (
    <Panel title="Miner log (archived)"
      right={(
        <span className="flex items-center gap-2">
          <span className="text-[11px] text-gray-500">as of {dt(log.data?.last_captured_at)}</span>
          <span className="text-[11px] text-gray-500">show</span>
          <select value={logSlice} onChange={(e) => setLogSlice(Number(e.target.value))}
            title="show only a slice of the log; last-N pins to the end, first-N to the top"
            className="rounded border border-gray-700 bg-[#0d0f12] px-1 py-0.5 text-[11px] text-gray-400">
            <option value={0}>full log</option>
            <option value={-50}>first 50 lines</option>
            {[50, 100, 200, 500].map((n) => (
              <option key={n} value={n}>last {n} lines</option>
            ))}
          </select>
          <button
            onClick={() => {
              const el = logRef.current
              if (el) el.scrollTop = el.scrollHeight
            }}
            className="rounded border border-gray-700 px-1.5 py-0.5 text-[11px] text-gray-400 hover:bg-gray-700/40 hover:text-gray-200">
            ↓ end
          </button>
        </span>
      )}>
      <pre ref={logRef} className="max-h-72 overflow-auto whitespace-pre-wrap break-all rounded bg-[#0d0f12] p-2 font-mono text-[11px] leading-4 text-gray-300">
        {shownLog || (log.isLoading ? 'loading…' : 'no archived log')}
      </pre>
    </Panel>
  )
}

type RigTab = 'charts' | 'details' | 'log'
const RIG_TABS: RigTab[] = ['charts', 'details', 'log']

function RigPanel({ rig }: { rig: Rig }) {
  const [tab, setTab] = useState<RigTab>('charts')
  return (
    <div className="mt-2 rounded-lg border border-teal-900/50 bg-[#14161a] p-3">
      <div className="mb-3 flex items-center gap-1 border-b border-gray-800 pb-2">
        <span className="mr-3 text-sm font-semibold text-gray-200">
          {rig.alias} <span className="text-gray-500">#{rig.order_id}</span>
        </span>
        {RIG_TABS.map((t) => (
          <button key={t} onClick={() => setTab(t)}
            className={`rounded px-3 py-1 text-sm transition-colors ${
              tab === t ? 'bg-teal-500/15 text-teal-300'
                        : 'text-gray-400 hover:bg-gray-800'}`}>
            {t}
          </button>
        ))}
      </div>
      {tab === 'details' && <RigDetails rig={rig} />}
      {tab === 'charts' && <RigCharts rig={rig} />}
      {tab === 'log' && <RigLog rig={rig} />}
    </div>
  )
}

export default function Fleet() {
  const fleet = useQuery({ queryKey: ['fleet'], queryFn: api.fleet, refetchInterval: POLL })
  const [showAll, setShowAll] = useState(false)
  const [openId, setOpenId] = useState<number | null>(null)

  const rows = (fleet.data ?? []).filter((r) =>
    showAll || r.visible !== false)

  const burnTotal = rows.reduce((s, r) => s + (r.rate_hr_usd ?? 0), 0)
  const spendTotal = rows.reduce((s, r) => s + (r.spend_usd ?? 0), 0)

  const selected = rows.find((r) => r.order_id === openId)

  return (
    <div>
      <div className="mb-2 flex items-center justify-between text-sm text-gray-400">
        <label className="flex cursor-pointer items-center gap-1.5">
          <input type="checkbox" checked={showAll} onChange={(e) => setShowAll(e.target.checked)}
            className="accent-teal-400" />
          show ended rigs ({rows.length})
        </label>
        <span className="tabular-nums">
          burn <b className="text-teal-300">${burnTotal.toFixed(3)}/hr</b>
          {' · '}spent <b className="text-gray-200">${spendTotal.toFixed(2)}</b>
        </span>
      </div>
      <Grid rows={rows} colDefs={COLS} height={selected ? 300 : 'calc(100vh - 170px)'}
        onRowClicked={(r) => setOpenId(openId === r.order_id ? null : r.order_id)} />
      {selected && <RigPanel rig={selected} />}
    </div>
  )
}
