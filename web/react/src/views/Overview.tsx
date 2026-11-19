import { useQuery } from '@tanstack/react-query'
import type { EChartsOption } from 'echarts'
import { api } from '../api'
import Chart, { baseGrid } from '../components/Chart'
import { Badge, Panel, Stat } from '../components/ui'
import { ago, dt, int, usd } from '../lib/format'

const POLL = 30_000

export default function Overview() {
  const summary = useQuery({ queryKey: ['summary'], queryFn: api.summary, refetchInterval: POLL })
  const pool = useQuery({ queryKey: ['poolSeries'], queryFn: api.poolSeries, refetchInterval: POLL })
  const fleet = useQuery({ queryKey: ['fleet'], queryFn: api.fleet, refetchInterval: POLL })
  const exp = useQuery({ queryKey: ['experiment', 'events'], queryFn: api.experimentEvents, refetchInterval: POLL })

  const s = summary.data
  const phase = s?.experiment.phase as Record<string, number> | null
  const points = pool.data?.points ?? []

  const poolOption: EChartsOption = {
    ...baseGrid(),
    series: [
      {
        name: 'pool sol/hr', type: 'line', showSymbol: false, smooth: true,
        data: points.map((p) => [p.source_ts, p.pool_sol_per_hour]),
        areaStyle: { opacity: 0.12 },
      },
      {
        name: 'est QUs', type: 'line', showSymbol: false, yAxisIndex: 1,
        data: points.map((p) => [p.source_ts, p.est_qus]),
      },
    ],
    yAxis: [
      { ...baseGrid().yAxis, name: 'sol/hr' },
      { ...baseGrid().yAxis, position: 'right', name: 'QUs', splitLine: { show: false } },
    ],
  }

  const rigs = fleet.data ?? []
  // same visibility rule as the Fleet table: provision_state AND the
  // order still existing in the last good inventory poll -- an
  // observe-only rig whose order vanished server-side is ended too
  const alive = rigs.filter((r) => r.visible !== false)

  return (
    <div className="space-y-3">
      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
        <Stat label="Balance" value={usd(s?.balance_usd, 2)} sub={`as of ${ago(s?.balance_at)}`} />
        <Stat label="Active rigs" value={String(s?.fleet_active ?? '—')}
          sub={`${s?.orders_active ?? 0} orders active`} />
        <Stat label="Submitted sh/hr" value={int(s?.shares_hr_total)}
          tone="good" sub="across active orders" />
        <Stat label="Marketplace" value={int(s?.available)}
          sub={`${int(s?.ranked)} ranked / ${int(s?.total)} total`} />
        <Stat label="Burn $/hr" value={usd(phase?.active_burn_hr as number, 3)}
          sub={phase ? `committed ${usd(phase.committed as number, 2)}` : 'no phase'} />
        <Stat label="Experiment"
          value={s?.experiment.runner_alive ? 'running' : 'idle'}
          tone={s?.experiment.runner_alive ? 'good' : 'default'}
          sub={phase ? `phase ${String(phase.phase_id)}` : undefined} />
      </div>

      <div className="grid grid-cols-1 gap-3 xl:grid-cols-3">
        <Panel title="Pool output" className="xl:col-span-2"
          right={<span className="text-[11px] text-gray-500">{points.length} snapshots</span>}>
          <Chart option={poolOption} height={280} />
        </Panel>
        <Panel title="Fleet">
          <div className="space-y-2">
            {alive.length === 0 && <p className="text-sm text-gray-500">no active rigs</p>}
            {alive.map((r) => (
              <div key={r.order_id}
                className="flex items-center justify-between rounded border border-gray-800 px-2 py-1.5">
                <div>
                  <div className="text-sm font-medium text-gray-100">{r.alias}</div>
                  <div className="text-[11px] text-gray-500">
                    {r.status?.hardware?.cpu_model ?? `server ${r.server_id}`}
                  </div>
                </div>
                <div className="text-right">
                  <div className="text-sm tabular-nums text-teal-300">
                    {r.status?.shares?.submitted ?? '—'} sh
                  </div>
                  <div className="text-[11px] text-gray-500">
                    it/s {r.status?.its?.gpu ?? '—'}/{r.status?.its?.cpu ?? '—'}
                  </div>
                </div>
              </div>
            ))}
          </div>
        </Panel>
      </div>

      <Panel title="Recent experiment events">
        <div className="max-h-64 space-y-1 overflow-y-auto text-sm">
          {(exp.data?.events ?? []).slice(0, 40).map((e) => (
            <div key={e.id} className="flex gap-3">
              <span className="w-36 shrink-0 text-gray-500">{dt(e.ts)}</span>
              <Badge tone={e.kind === 'tick' ? 'gray' : 'indigo'}>{e.kind}</Badge>
              <span className="truncate text-gray-300">
                {e.order_id ? `#${e.order_id} ` : ''}
                {Object.entries(e.detail ?? {}).map(([k, v]) => `${k}=${v}`).join(' ')}
              </span>
            </div>
          ))}
        </div>
      </Panel>
    </div>
  )
}
