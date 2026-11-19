import { useEffect, useMemo, useState, type ReactNode } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import type { ColDef } from 'ag-grid-community'
import { api } from '../api'
import type { Candidate, ExpEvent, ExpRental, GpuStat,
  PoolShareEstimate, Verdict } from '../api'
import { Badge, Grid, Panel, Stat, pinColumn } from '../components/ui'
import { ago, dt, num, usd } from '../lib/format'

const POLL = 30_000

const CAND_COLS: ColDef<Candidate>[] = [
  { field: 'server_id', headerName: 'Server', width: 95 },
  {
    field: 'gpu', headerName: 'GPU', width: 150,
    valueFormatter: (p) => p.value ?? '—',
  },
  {
    field: 'cpu', headerName: 'CPU', width: 190,
    valueFormatter: (p) => p.value ?? '—',
  },
  {
    field: 'meas_rentals', headerName: 'rents', width: 70, type: 'numericColumn',
    valueFormatter: (p) => (p.value ? String(p.value) : '—'),
  },
  {
    field: 'meas_its', headerName: 'meas it/s', width: 95, type: 'numericColumn',
    cellRenderer: (p: { value: number | null; data?: Candidate }) => (
      <span title={p.data?.meas_cross_epoch
        ? 'measured on this server in a previous epoch'
        : 'median it/s measured on this server'}>
        {p.value != null ? num(p.value, 0) : '—'}
        {p.data?.meas_cross_epoch ? ' †' : ''}
      </span>
    ),
  },
  {
    field: 'est_its', headerName: 'est it/s', width: 95, type: 'numericColumn',
    cellRenderer: (p: { value: number | null; data?: Candidate }) => (
      <span>{num(p.value, 0)}{p.data?.extrapolated ? ' ~' : ''}</span>
    ),
  },
  {
    field: 'hourly', headerName: 'cost $/hr', width: 95, type: 'numericColumn',
    cellRenderer: (p: { value: number | null; data?: Candidate }) => (
      <span title={p.data?.mode === 'spot' ? 'spot bid' : 'on-demand'}>
        {num(p.value, 4)}{p.data?.mode === 'spot' ? ' s' : ''}
      </span>
    ),
  },
  {
    field: 'usd_hr', headerName: 'rev $/hr', width: 90, type: 'numericColumn',
    valueFormatter: (p) => num(p.value, 3),
  },
  {
    field: 'return_x', headerName: 'return×', width: 95, type: 'numericColumn',
    valueFormatter: (p) => num(p.value, 2),
    cellStyle: (p) => (p.value != null && p.value > 1.5 ? { color: '#2dd4bf' } : undefined),
  },
  {
    field: 'net_hr', headerName: 'net $/hr', width: 100, type: 'numericColumn',
    valueFormatter: (p) => num(p.value, 3),
  },
  {
    field: 'prior', headerName: 'prior', width: 90, type: 'numericColumn',
    valueFormatter: (p) => num(p.value, 2),
  },
  { field: 'fill_status', headerName: 'Fill', width: 110 },
  {
    field: 'upgrade_status', headerName: 'Upgrade', width: 100,
    cellRenderer: (p: { value: string | null }) =>
      p.value === 'tried' ? <span className="text-amber-400/80">{p.value}</span> : p.value,
  },
]

const RENTAL_COLS: ColDef<ExpRental>[] = [
  { field: 'order_id', headerName: 'Order', width: 95 },
  { field: 'server_id', headerName: 'Server', width: 90 },
  { field: 'role', headerName: 'Role', width: 80 },
  {
    field: 'mode', headerName: 'Mode', width: 75,
    valueFormatter: (p) => (p.value === 'spot' ? 'spot' : '—'),
  },
  {
    field: 'tier', headerName: 'Tier', width: 80,
    cellRenderer: (p: { value: string | null }) =>
      p.value ? <Badge tone={p.value === 'anchor' ? 'teal' : 'gray'}>{p.value}</Badge> : '',
  },
  {
    field: 'cpu', headerName: 'CPU', flex: 1.5, minWidth: 160,
    valueFormatter: (p) => (p.value ?? '').replace(/\(R\)|\(TM\)| CPU | Processor/g, '').slice(0, 30),
  },
  { field: 'gpu', headerName: 'GPU', width: 110 },
  {
    field: 'hourly_usd', headerName: '$/hr', width: 85, type: 'numericColumn',
    valueFormatter: (p) => num(p.value, 3),
  },
  {
    field: 'its_total', headerName: 'it/s', width: 80, type: 'numericColumn',
    valueFormatter: (p) => num(p.value, 0),
  },
  {
    field: 'forecast_return_x', headerName: 'EstRet', width: 90, type: 'numericColumn',
    valueFormatter: (p) => (p.value == null ? '—' : `~${Number(p.value).toFixed(2)}x`),
    cellStyle: (p) => (p.value != null && p.value > 1.5 ? { color: '#2dd4bf' } : undefined),
  },
  {
    field: 'forecast_usd_hr', headerName: 'Gross$/h', width: 95, type: 'numericColumn',
    valueFormatter: (p) => (p.value == null ? '—' : `~$${Number(p.value).toFixed(3)}`),
  },
  {
    field: 'forecast_net_hr', headerName: 'Net$/h', width: 90, type: 'numericColumn',
    valueFormatter: (p) => (p.value == null ? '—' : `~$${Number(p.value).toFixed(3)}`),
  },
  {
    field: 'age_s', headerName: 'Age', width: 80,
    valueFormatter: (p) => {
      const s = Number(p.value)
      return Number.isFinite(s) ? `${Math.round(s / 60)}m` : '—'
    },
  },
  {
    field: 'shares', headerName: 'Shares', width: 85, type: 'numericColumn',
    cellStyle: { color: '#2dd4bf' },
  },
  {
    field: 'last_share_at', headerName: 'Last share', width: 100,
    valueFormatter: (p) => ago(p.value),
  },
  {
    field: 'tree_id', headerName: 'Tree', width: 140,
    cellRenderer: (p: { value: string | null; data?: ExpRental }) => (
      <span className="inline-flex items-center gap-1">
        {p.value ?? '—'}
        {p.data?.tree_wait && (
          <Badge tone="amber">paused</Badge>
        )}
      </span>
    ),
  },
  { field: 'next', headerName: 'Next', flex: 1, minWidth: 140 },
]

const EVENT_COLS: ColDef<ExpEvent>[] = [
  { field: 'ts', headerName: 'Time', width: 165, valueFormatter: (p) => dt(p.value) },
  {
    field: 'kind', headerName: 'Kind', width: 110,
    cellRenderer: (p: { value: string }) =>
      <Badge tone={p.value === 'tick' ? 'gray' : 'indigo'}>{p.value}</Badge>,
  },
  {
    field: 'order_id', headerName: 'Order', width: 90,
    valueFormatter: (p) => p.value ?? '',
  },
  {
    field: 'server_id', headerName: 'Server', width: 90,
    valueFormatter: (p) => p.value ?? '',
  },
  {
    field: 'detail', headerName: 'Detail', flex: 3, minWidth: 300, sortable: false, filter: false,
    valueFormatter: (p) =>
      Object.entries(p.value ?? {}).map(([k, v]) => `${k}=${v}`).join('  '),
  },
]

function RentalsPanel({ phaseLabel }: { phaseLabel: string }) {
  const rentals = useQuery({ queryKey: ['experiment', 'rentals'], queryFn: api.experimentRentals, refetchInterval: POLL })
  return (
    <Panel title={`Rentals (${rentals.data?.rentals?.length ?? '…'})`}
      right={<span className="text-[11px] text-gray-500">{phaseLabel}</span>}>
      {rentals.data
        ? <Grid rows={rentals.data.rentals ?? []} colDefs={RENTAL_COLS} height={420} />
        : <p className="text-gray-500">loading…</p>}
    </Panel>
  )
}

function CandidatesPanel() {
  const qc = useQueryClient()
  const cands = useQuery({ queryKey: ['experiment', 'candidates'], queryFn: api.candidates, refetchInterval: POLL })
  const pins = useQuery({ queryKey: ['probePins'], queryFn: api.probePins, refetchInterval: POLL })
  const pinSet = useMemo(
    () => new Set((pins.data?.pins ?? []).map((p) => p.server_id)),
    [pins.data])
  const [rentedOnly, setRentedOnly] = useState(false)
  const cols = useMemo<ColDef<Candidate>[]>(() => [
    pinColumn<Candidate>(pinSet, (sid, on) =>
      api.setProbePin(sid, on)
        .then(() => qc.invalidateQueries({ queryKey: ['probePins'] }))),
    ...CAND_COLS], [pinSet, qc])
  const rows = useMemo(
    () => (cands.data?.candidate_values ?? [])
      .filter((c) => !rentedOnly || c.meas_rentals > 0),
    [cands.data, rentedOnly])
  const unlistedPins = pinSet.size
    - rows.reduce((n, r) => n + (pinSet.has(r.server_id) ? 1 : 0), 0)
  return (
    <Panel title={`Candidates (${rows.length})`}
      right={<span className="text-[11px] text-gray-500">
        <label className="mr-2 inline-flex cursor-pointer items-center gap-1">
          <input type="checkbox" checked={rentedOnly}
            onChange={(e) => setRentedOnly(e.target.checked)}
            className="accent-teal-400" />
          rented before
        </label>
        {pinSet.size > 0
          ? `${pinSet.size} pinned for next probe` +
            (unlistedPins ? ` (${unlistedPins} not in table)` : '') + ' · '
          : ''}
        ★ = audition next
      </span>}>
      {cands.data
        ? <Grid rows={rows} colDefs={cols} height={420} />
        : <p className="text-gray-500">loading…</p>}
    </Panel>
  )
}

function PriorsPanel() {
  const cands = useQuery({ queryKey: ['experiment', 'candidates'], queryFn: api.candidates, refetchInterval: POLL })
  const gpuRows = useMemo(
    () => Object.entries(cands.data?.gpu_stats ?? {})
      .map(([gpu, s]: [string, GpuStat]) => ({ gpu, ...s }))
      .sort((a, b) => (b.prior ?? -1) - (a.prior ?? -1)
        || b.rentals - a.rentals),
    [cands.data])
  return (
    <Panel title={`GPU priors (${gpuRows.length})`}
      right={<span className="text-[11px] text-gray-500">
        share-proven hardware history · † older-epoch
      </span>}>
      {cands.data == null
        ? <p className="text-gray-500">loading…</p>
        : (
          <div className="text-xs">
            <table className="w-full max-w-4xl tabular-nums">
              <thead>
                <tr className="border-b border-gray-800 text-left text-gray-500">
                  <th className="py-1 font-normal">GPU</th>
                  <th className="py-1 text-right font-normal"
                    title="median it/s per rental, share-proven only">
                    it/s</th>
                  <th className="py-1 text-right font-normal"
                    title="median live on-demand ask per card">
                    $/hr</th>
                  <th className="py-1 text-right font-normal"
                    title="pool-weighted sol per raw submitted share — ~1.0 means the pool credits each share equally">
                    wt</th>
                  <th className="py-1 text-right font-normal"
                    title="mean ReportedScore per submitted share — ant-quality signal behind the weight">
                    sps</th>
                  <th className="py-1 text-right font-normal"
                    title="total weighted sol across measured stints">
                    sol</th>
                  <th className="py-1 text-right font-normal"
                    title="pool-reported shares found this epoch">
                    epoch sh</th>
                  <th className="py-1 text-right font-normal"
                    title="stints with sol data / share-proven rentals / total rentals">
                    w/prv/tot</th>
                </tr>
              </thead>
              <tbody>
                {gpuRows.map((g) => (
                  <tr key={g.gpu}
                    className={`border-b border-gray-900 ${g.prior == null
                      ? 'text-gray-600' : 'text-gray-200'}`}>
                    <td className="py-1">
                      {g.gpu}{g.cross_epoch ? ' †' : ''}
                    </td>
                    <td className="py-1 text-right">
                      {g.prior != null ? num(g.prior, 0) : '—'}
                    </td>
                    <td className="py-1 text-right"
                      title={g.listings
                        ? `median on-demand ask per card, ${g.listings} live listings`
                        : 'no live listings'}>
                      {g.median_hr != null ? num(g.median_hr, 2) : '—'}
                    </td>
                    <td className="py-1 text-right"
                      title={g.weight_n
                        ? `sol/submitted over ${g.weight_n} stints`
                        : 'no sol-windowed stints'}>
                      {g.weight != null ? num(g.weight, 2) : '—'}
                    </td>
                    <td className="py-1 text-right"
                      title={g.score_n
                        ? `score per submitted share over ${g.score_n} scored stint${g.score_n === 1 ? '' : 's'}`
                        : 'no score-reporting stints'}>
                      {g.score_per_share != null
                        ? num(g.score_per_share, 0) : '—'}
                    </td>
                    <td className="py-1 text-right">
                      {g.sol ? num(g.sol, 0) : '—'}
                    </td>
                    <td className="py-1 text-right">
                      {g.shares}
                    </td>
                    <td className="py-1 text-right text-gray-500">
                      {g.weight_n ?? 0}/{g.proven}/{g.rentals}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <div className="mt-2 max-w-3xl text-[10px] leading-relaxed text-gray-600">
              it/s: median per-rental, share-proven only ·
              $/hr: median live on-demand ask ·
              wt: pool sol ÷ miner-submitted shares per stint,
              reset-segmented (per-session sol, ~10min snapshot cadence —
              short stints can miss their sol peak and read low) ·
              sps: mean ReportedScore per submitted share
              (score-weighted yield normalizes against the fleet
              median of this) ·
              sol: summed weighted sol ·
              w/prv/tot = sol-covered stints / share-proven / all rentals ·
              † older-epoch · — no proven prior (extrapolated)
            </div>
          </div>
        )}
    </Panel>
  )
}

function EventsPanel() {
  const events = useQuery({ queryKey: ['experiment', 'events'], queryFn: api.experimentEvents, refetchInterval: POLL })
  const evs = events.data?.events
  return (
    <Panel title={`Events (${evs?.length ?? '…'})`}
      right={<span className="text-[11px] text-gray-500">latest first</span>}>
      {evs
        ? <Grid rows={evs} colDefs={EVENT_COLS} height={420} />
        : <p className="text-gray-500">loading…</p>}
    </Panel>
  )
}

const VERDICT_TONE: Record<string, 'teal' | 'amber' | 'red' | 'gray'> = {
  promoted: 'teal', displaced: 'teal', replaced: 'teal',
  renew: 'teal', kept: 'teal',
  probe_lost: 'amber', probe_deadline: 'amber', probe_no_share: 'amber',
  dry: 'amber', same_tree: 'amber', low_yield: 'amber',
  unreachable: 'red', fault: 'red', provision_failed: 'red',
}
function verdictTone(v: string | null): 'teal' | 'amber' | 'red' | 'gray' {
  if (!v) return 'gray'
  if (v.startsWith('fault')) return 'red'
  return VERDICT_TONE[v] ?? 'gray'
}

const VERDICT_COLS: ColDef<Verdict>[] = [
  { field: 'ended_at', headerName: 'Ended', width: 165, valueFormatter: (p) => dt(p.value) },
  { field: 'order_id', headerName: 'Order', width: 95 },
  { field: 'server_id', headerName: 'Server', width: 90 },
  { field: 'role', headerName: 'Role', width: 80 },
  {
    field: 'mode', headerName: 'Mode', width: 75,
    valueFormatter: (p) => (p.value === 'spot' ? 'spot' : '—'),
  },
  {
    field: 'verdict', headerName: 'Verdict', width: 130,
    cellRenderer: (p: { value: string | null; data?: Verdict }) => (
      <span title={p.data?.decided ? `decided: ${p.data.decided}` : undefined}>
        <Badge tone={verdictTone(p.value)}>{p.value ?? '—'}</Badge>
      </span>
    ),
  },
  { field: 'its', headerName: 'it/s', width: 80, type: 'numericColumn', valueFormatter: (p) => num(p.value, 0) },
  {
    field: 'shares', headerName: 'Shares', width: 85, type: 'numericColumn',
    cellStyle: { color: '#2dd4bf' },
  },
  {
    field: 'age_min', headerName: 'Age', width: 75, type: 'numericColumn',
    valueFormatter: (p) => (p.value == null ? '—' : `${p.value}m`),
  },
  {
    field: 'cost_usd', headerName: 'Cost', width: 85, type: 'numericColumn',
    valueFormatter: (p) => usd(p.value, 2),
  },
  { field: 'outcome', headerName: 'Burst outcome', flex: 2, minWidth: 220, valueFormatter: (p) => p.value ?? '' },
]

function VerdictsPanel() {
  const verdicts = useQuery({
    queryKey: ['experiment', 'verdicts'],
    queryFn: api.experimentVerdicts, refetchInterval: POLL })
  const rows = verdicts.data?.verdicts
  return (
    <Panel title={`Verdicts (${rows?.length ?? '…'})`}
      right={<span className="text-[11px] text-gray-500">
        ended rentals, newest first — burst judgements inline
      </span>}>
      {verdicts.isError
        ? <p className="text-red-400">error: {String(verdicts.error)}</p>
        : rows == null
          ? <p className="text-gray-500">loading…</p>
          : rows.length === 0
            ? <p className="text-gray-500">no ended rentals yet</p>
            : <Grid rows={rows} colDefs={VERDICT_COLS} height={420} />}
    </Panel>
  )
}

function useDismissed(storageKey: string, ev: ExpEvent | undefined) {
  const key = ev ? String(ev.id) : null
  const [dismissed, setDismissed] = useState<string | null>(
    () => localStorage.getItem(storageKey))
  const dismiss = () => {
    if (key == null) return
    localStorage.setItem(storageKey, key)
    setDismissed(key)
  }
  return { show: key != null && key !== dismissed, dismiss }
}

function AlertShell({ tone, onDismiss, children }: {
  tone: 'red' | 'teal'
  onDismiss: () => void
  children: ReactNode
}) {
  const box = tone === 'red'
    ? 'border-red-900/60 bg-red-950/30 text-red-300'
    : 'border-teal-900/60 bg-teal-950/30 text-teal-300'
  const btn = tone === 'red'
    ? 'border-red-800 hover:bg-red-900/40'
    : 'border-teal-800 hover:bg-teal-900/40'
  return (
    <div className={`flex items-start justify-between gap-3 rounded-lg border px-3 py-2 ${box}`}>
      <p className="text-sm">{children}</p>
      <button onClick={onDismiss}
        className={`shrink-0 rounded border px-2 py-0.5 text-xs ${btn}`}>
        dismiss
      </button>
    </div>
  )
}

function NoVacancyAlert({ evs }: { evs: ExpEvent[] | undefined }) {
  const nv = evs?.find(
    (e) => e.kind === 'burst_judged' && e.detail?.verdict === 'no_vacancy')
  const { show, dismiss } = useDismissed('experiment.novac.dismissed', nv)
  if (!show || !nv) return null
  const stats = (nv.detail?.stats ?? {}) as Record<string, number | null>
  const dropped = (((nv.detail?.cancelled ?? nv.detail?.finders) as number[] | undefined) ?? [])[0]
  return (
    <AlertShell tone="red" onDismiss={dismiss}>
      <span className="font-semibold">winner dropped — no vacancy.</span>{' '}
      probe {dropped ?? nv.order_id ?? '?'} won its audition
      ({num(stats.probe_its, 0)} it/s, est {usd(stats.probe_net_usd_hr, 2)}/h
      vs bar {usd(stats.seat_bar, 2)}/h) but every seat was full — bump
      max_rentals to seat future winners.{' '}
      <span className="opacity-60">{ago(nv.ts)}</span>
    </AlertShell>
  )
}

function GrowthAlert({ evs }: { evs: ExpEvent[] | undefined }) {
  const ev = evs?.find(
    (e) => e.kind === 'burst_judged' &&
      (((e.detail?.promoted_extra ?? []) as { via?: string }[])
        .some((p) => p.via === 'growth')))
  const { show, dismiss } = useDismissed('experiment.growth.dismissed', ev)
  if (!show || !ev) return null
  const promoted = (ev.detail?.promoted_extra ?? []) as
    { order_id: number; server_id: number; tier?: string }[]
  return (
    <AlertShell tone="teal" onDismiss={dismiss}>
      <span className="font-semibold">growth seat earned.</span>{' '}
      {promoted.map((p) => `probe ${p.order_id} → slot (s${p.server_id})`)
        .join(', ')}{' '}
      — fleet expanded to seat a proven winner, burst{' '}
      {String(ev.detail?.burst_id ?? '?')}.{' '}
      <span className="opacity-60">{ago(ev.ts)}</span>
    </AlertShell>
  )
}

type ExpTab = 'rentals' | 'verdicts' | 'candidates' | 'priors' | 'events'
const EXP_TABS: ExpTab[] = ['rentals', 'verdicts', 'candidates', 'priors', 'events']

function PoolSharePanel({ ps, cfgPct }: {
  ps: PoolShareEstimate | null | undefined
  cfgPct: number | null | undefined
}) {
  const qc = useQueryClient()
  const [val, setVal] = useState('')
  const [msg, setMsg] = useState('')
  const [busy, setBusy] = useState(false)
  useEffect(() => {
    if (cfgPct != null) setVal(String(cfgPct))
  }, [cfgPct])
  const save = () => {
    const n = val.trim() === '' ? 0 : Number(val)
    if (!Number.isFinite(n) || n < 0 || n > 100) {
      setMsg('target must be 0–100')
      return
    }
    setBusy(true)
    setMsg('')
    api.updateConfig({ pool_share_target_pct: n }).then(() => {
      setMsg('saved')
      qc.invalidateQueries({ queryKey: ['config'] })
      qc.invalidateQueries({ queryKey: ['experiment'] })
    }).catch((e) => setMsg(String(e.message ?? e)))
      .finally(() => setBusy(false))
  }
  const usdOr = (v: number | null | undefined, p = 2) =>
    v == null ? '—' : usd(v, p)
  const approve = () => {
    setBusy(true)
    setMsg('')
    api.approvePoolShare().then((r) => {
      setMsg(`approved — budget now $${r.budget_usd}`)
      qc.invalidateQueries({ queryKey: ['config'] })
      qc.invalidateQueries({ queryKey: ['experiment'] })
    }).catch((e) => setMsg(String(e.message ?? e)))
      .finally(() => setBusy(false))
  }
  const app = ps?.approved
  return (
    <Panel title="pool share target">
      <div className="flex flex-wrap items-end gap-x-6 gap-y-2 text-sm">
        <label className="text-xs text-gray-500">
          target %
          <input
            className="ml-1 w-16 rounded border border-gray-700
              bg-gray-900 px-1.5 py-0.5 text-right text-gray-200"
            value={val} inputMode="decimal" disabled={busy}
            onChange={(e) => setVal(e.target.value)}
            onBlur={() => { if (val !== String(cfgPct ?? '')) save() }}
            onKeyDown={(e) => { if (e.key === 'Enter') save() }} />
        </label>
        <div>
          <span className="text-gray-500">now </span>
          {ps?.share_now_pct != null
            ? `${num(ps.share_now_pct, 2)}%` : '—'}
          <span className="text-gray-500">
            {` (${num(ps?.our_sol ?? 0, 0)} sol / ${num(ps?.total_sol ?? 0, 0)})`}
          </span>
        </div>
        {ps && !ps.enabled && (
          <span className="text-xs text-amber-400">
            off — what-if estimate; enable in Settings › Strategy to arm
          </span>
        )}
        <div>
          <span className="text-gray-500">needs </span>
          {ps?.needed_its != null
            ? `${num(ps.needed_its, 0)} it/s (${num(ps.needed_sol ?? 0, 0)} sol)`
            : '—'}
          {(ps?.deficit_its ?? 0) > 0 && (
            <span className="text-amber-300">
              {` · behind by ${num(ps.deficit_its, 0)} it/s`}
              {ps?.enabled && app && app.usd > 0
                ? ' → catch-up pricing armed' : ''}
            </span>
          )}
        </div>
        <div>
          <span className="text-gray-500">est </span>
          {`${usdOr(ps?.est_cost_hr)}/hr · ${usdOr(ps?.est_cost_day)}/day · `}
          {ps?.epoch_h_left != null
            ? `${usdOr(ps.est_cost_epoch)} over ${num(ps.epoch_h_left, 0)}h left`
            : '—'}
        </div>
        {ps?.enabled && ps.est_cost_epoch != null && (
          <button onClick={approve} disabled={busy}
            className="rounded bg-teal-600 px-2.5 py-1 text-xs font-semibold
              text-white disabled:opacity-50"
            title="sets phase budget = committed + estimated epoch cost">
            approve → fund {usdOr(ps.est_cost_epoch)}
          </button>
        )}
        {msg && <span className="text-teal-400">{msg}</span>}
      </div>
      {app != null && app.usd > 0 && (
        <div className="mt-1 flex flex-wrap gap-x-4 text-[11px]">
          <span className="text-gray-400">
            approved {usdOr(app.usd)} @ {num(app.pct, 2)}%
            {app.at ? ` · ${ago(app.at)}` : ''}
            {` (${usdOr(app.hr)}/hr then)`}
          </span>
          {ps?.reapproval_needed && (
            <span className="text-amber-400">
              target or cost moved &gt;{''}
              {Math.round((ps.cost_drift ?? 0) * 100)}% vs approved — re-approve
            </span>
          )}
        </div>
      )}
      {(ps?.pace_deficit || (ps?.supply_short_its ?? 0) > 0) && (
        <div className="mt-1 space-y-0.5 text-[11px]">
          {ps?.pace_deficit && (
            <p className="text-red-400">
              remaining budget can't fund the target to epoch end —
              re-approve or lower the %
            </p>
          )}
          {(ps?.supply_short_its ?? 0) > 0 && (
            <p className="text-red-400">
              under-cap supply reaches only {num(ps.cap_ceiling_its ?? 0)} it/s
              — target needs +{num(ps.supply_short_its, 0)} more;
              raise pool_share_max_price_hr or lower the %
            </p>
          )}
        </div>
      )}
      <div className="mt-1 text-[10px] text-gray-600">
        {ps?.affordable_pct != null && (
          <span className="text-gray-500">
            {`remaining buys ~${num(ps.affordable_pct, 2)}% · `}
          </span>
        )}
        cost basis: {ps?.cost_basis === 'supply_curve'
          ? 'marketplace supply curve (share cap)'
          : ps?.cost_basis === 'measured_eff'
            ? `fleet-measured ${num(ps.eff_its_per_usd ?? 0, 0)} it/s per $/hr`
            : '—'}
        {` · sol/its ${ps?.sol_per_its != null ? num(ps.sol_per_its, 2) : '—'}`}
        {` · fleet ${num(ps?.fleet_its ?? 0, 0)} it/s`}
        {` · pool snap ${ps?.snapshot_age_s != null
          ? `${Math.round(ps.snapshot_age_s / 60)}m old` : '—'}`}
        {' · estimator only — share presence is window-lagged (~hours)'}
      </div>
    </Panel>
  )
}

export default function Experiment() {
  // one query per panel: sections load independently and share the
  // backend's cached build_state pass; events bypasses it entirely
  const status = useQuery({ queryKey: ['experiment', 'status'], queryFn: api.experimentStatus, refetchInterval: POLL })
  const events = useQuery({ queryKey: ['experiment', 'events'], queryFn: api.experimentEvents, refetchInterval: POLL })
  const pins = useQuery({ queryKey: ['probePins'], queryFn: api.probePins, refetchInterval: POLL })
  const cfg = useQuery({ queryKey: ['config'], queryFn: api.config, refetchInterval: POLL })
  const nPins = pins.data?.pins?.length ?? 0
  const [tab, setTab] = useState<ExpTab>('candidates')
  const d = status.data
  const ph = (d?.phase ?? {}) as Record<string, number | string | null>
  const evs = events.data?.events
  const upgradeScan = evs?.find(
    (e) => e.kind === 'note' &&
      String(e.detail?.text ?? '').startsWith('upgrade scan:'))
  // the bar a new seat must clear: weakest held seat's $-score at the
  // latest judgement, floored by min_seat_return on the return side
  const seatEv = evs?.find(
    (e) => e.kind === 'burst_judged' &&
      (e.detail?.stats as { seat_bar?: number | null } | undefined)
        ?.seat_bar != null)
  const seatBar = (seatEv?.detail?.stats as { seat_bar?: number | null } | undefined)?.seat_bar ?? null

  return (
    <div className="space-y-3">
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-8">
        <Stat label="Runner"
          value={status.isLoading ? '…' : (d?.runner_alive ? 'alive' : 'stopped')}
          tone={d?.runner_alive ? 'good' : 'warn'} />
        <Stat label="Budget" value={usd(Number(ph.budget), 2)}
          sub={`reserve ${usd(Number(ph.reserve), 2)}`} />
        <Stat label="Committed" value={usd(Number(ph.committed), 2)} />
        <Stat label="Remaining" value={usd(Number(ph.remaining), 2)} />
        <Stat label="Burn $/hr" value={usd(Number(ph.active_burn_hr), 3)}
          sub={`optimal ${usd(Number(ph.optimal_burn_hr), 3)}`} />
        <Stat label="Headroom" value={usd(Number(ph.headroom_hr), 3)} sub="$/hr" />
        <Stat label="Seat bar"
          value={seatBar != null ? usd(seatBar, 2) + '/h' : '—'}
          sub={(cfg.data?.min_seat_return != null
            ? `floor ${num(cfg.data.min_seat_return, 2)}× ret · `
            : '') + 'weakest seat'} />
        <Stat label="Phase ends" value={String(ph.ends_at ?? '—').slice(5, 16)}
          sub={`started ${String(ph.started_at ?? '—').slice(5, 16)}`} />
      </div>

      <PoolSharePanel ps={d?.pool_share}
        cfgPct={cfg.data?.pool_share_target_pct} />

      {(upgradeScan || d?.offering_dead ||
        (d?.pending_rerents?.length ?? 0) > 0 ||
        (d?.bursts?.length ?? 0) > 0 || d?.burst) && (
        <Panel>
          <div className="space-y-1 text-sm">
            {d?.offering_dead && (
              <p className="text-amber-400">
                probes paused — scout {String(d.agent?.source ?? '?')} on
                dead tree {String(d.agent?.tree_id ?? '?')}; waiting for
                rotation (~{Math.round(Number(d.tree_lifetime_min ?? 20))}m)
              </p>
            )}
            {(d?.bursts ?? (d?.burst != null ? [d.burst] : [])).map((b, i) => (
              <p key={i} className="text-indigo-300">
                burst {String(b.burst_id)} ({String(b.trigger)}) —{' '}
                {(b.probes as unknown[])?.length ?? 0}/{String(b.n_target)} probes,
                judge in {Math.round(Number(b.judge_in_s ?? 0) / 60)}m
              </p>
            ))}
            {(d?.pending_rerents ?? []).map((r, i) => {
              const rr = r as Record<string, unknown>
              return (
                <p key={i} className="text-amber-400">
                  rerent {String(rr.server_id)} due {dt(String(rr.due_at))}
                </p>
              )
            })}
            {upgradeScan && (
              <p className="text-gray-300">
                <span className="text-gray-500">latest upgrade check: </span>
                {String((upgradeScan.detail ?? {}).text ?? '')}
                <span className="text-gray-500"> · {ago(upgradeScan.ts)}</span>
              </p>
            )}
          </div>
        </Panel>
      )}

      <NoVacancyAlert evs={evs} />
      <GrowthAlert evs={evs} />

      <div className="rounded-lg border border-teal-900/50 bg-[#14161a] p-3">
        <div className="mb-3 flex items-center gap-1 border-b border-gray-800 pb-2">
          {EXP_TABS.map((t) => (
            <button key={t} onClick={() => setTab(t)}
              className={`rounded px-3 py-1 text-sm transition-colors ${
                tab === t ? 'bg-teal-500/15 text-teal-300'
                          : 'text-gray-400 hover:bg-gray-800'}`}>
              {t}{t === 'candidates' && nPins > 0 ? ` ★${nPins}` : ''}
            </button>
          ))}
        </div>
        {tab === 'rentals' && (
          <RentalsPanel
            phaseLabel={String(ph.phase_id ? `phase ${ph.phase_id}` : '')} />
        )}
        {tab === 'verdicts' && <VerdictsPanel />}
        {tab === 'candidates' && <CandidatesPanel />}
        {tab === 'priors' && <PriorsPanel />}
        {tab === 'events' && <EventsPanel />}
      </div>
    </div>
  )
}
