import { useQuery, useQueryClient } from '@tanstack/react-query'
import type { EChartsOption } from 'echarts'
import { useState } from 'react'
import { api } from '../api'
import type { MarketRow } from '../api'
import Chart, { baseGrid } from './Chart'
import { KV, Panel } from './ui'
import { RentModal } from './RentModal'
import { dt, num, usd } from '../lib/format'

export default function ServerDrawer({ serverId, row, onClose }: {
  serverId: number
  row?: MarketRow
  onClose: () => void
}) {
  const detail = useQuery({
    queryKey: ['server', serverId],
    queryFn: () => api.server(serverId),
  })
  const [rentOpen, setRentOpen] = useState(false)
  const [blConfirm, setBlConfirm] = useState(false)
  const [blBusy, setBlBusy] = useState(false)
  const [blErr, setBlErr] = useState('')
  const [blRemoved, setBlRemoved] = useState(false)
  const qc = useQueryClient()
  const bl = row?.blacklist

  function unblock() {
    if (!blConfirm) { setBlConfirm(true); return }
    setBlBusy(true)
    setBlErr('')
    api.removeBlacklist(serverId)
      .then(() => {
        setBlRemoved(true)
        qc.invalidateQueries({ queryKey: ['market'] })
        qc.invalidateQueries({ queryKey: ['candidates'] })
      })
      .catch((e) => setBlErr(String(e.message ?? e)))
      .finally(() => setBlBusy(false))
  }
  const d = detail.data
  const listing = (d?.listing ?? {}) as Record<string, unknown>
  const specs = (listing.specs ?? {}) as Record<string, unknown>
  const usdPrice = (listing.price ?? {}) as Record<string, Record<string, unknown>>
  const mrl = (() => {
    const v = Number(listing.mrl)
    return Number.isInteger(v) && v >= 1 ? v : null
  })()

  const priceOption: EChartsOption = {
    ...baseGrid(),
    series: [{
      name: '$/day', type: 'line', step: 'end', showSymbol: true, symbolSize: 5,
      data: (d?.prices ?? []).map((p) => [p.observed_at, p.on_demand_usd_day]),
      areaStyle: { opacity: 0.1 },
    }],
  }

  return (
    <div className="fixed inset-y-0 right-0 z-40 flex w-full max-w-xl flex-col border-l border-gray-700 bg-[#14161a] shadow-2xl">
      <header className="flex items-center justify-between border-b border-gray-800 px-4 py-2">
        <h2 className="text-lg font-bold text-teal-300">
          server {serverId}
          <span className="ml-2 text-sm font-normal text-gray-500">
            owner {d?.owner} · {d?.country}
          </span>
        </h2>
        <div className="flex items-center gap-2">
          <button
            onClick={() => setRentOpen(true)}
            className="rounded bg-teal-600 px-3 py-1 text-sm font-semibold text-white hover:bg-teal-500">
            Rent…
          </button>
          <button onClick={onClose} className="rounded px-2 py-1 text-gray-400 hover:bg-gray-800">✕</button>
        </div>
      </header>
      <div className="flex-1 space-y-3 overflow-y-auto p-4">
        {detail.isLoading && <p className="text-gray-500">loading…</p>}
        {detail.isError && <p className="text-red-400">failed to load</p>}
        {bl && (
          <Panel title="Blacklist">
            <div className="space-y-2 text-sm">
              <p className="text-red-400">{bl.reason}</p>
              <p className="text-xs text-gray-500">
                {bl.source} · added {dt(bl.added_at)}
                {bl.expires_at ? ` · expires ${dt(bl.expires_at)}` : ' · never expires'}
              </p>
              {blRemoved ? (
                <p className="text-teal-300">removed — grid updates on next refresh</p>
              ) : (
                <div className="flex items-center gap-2">
                  <button onClick={unblock} disabled={blBusy}
                    className={`rounded px-3 py-1 text-sm font-semibold ${
                      blConfirm
                        ? 'bg-red-600 text-white hover:bg-red-500'
                        : 'border border-red-800 text-red-400 hover:bg-red-500/10'
                    } disabled:opacity-50`}>
                    {blBusy ? 'removing…' : blConfirm ? 'Confirm remove' : 'Remove from blacklist'}
                  </button>
                  {blConfirm && !blBusy && (
                    <button onClick={() => setBlConfirm(false)}
                      className="text-xs text-gray-500 hover:text-gray-300">
                      cancel
                    </button>
                  )}
                  {blErr && <span className="text-xs text-red-400">{blErr}</span>}
                </div>
              )}
            </div>
          </Panel>
        )}
        {d && (
          <>
            <Panel title="Current listing">
              <KV items={[
                ['GPU', ((specs.gpu_array as string[]) ?? []).join(', ') || '—'],
                ['CPU', String(specs.cpu ?? '—')],
                ['threads', String(specs.cpus ?? '—')],
                ['RAM', `${String(specs.ram ?? '—')} GB`],
                ['disk', `${String(specs.disk ?? '—')} GB (${String(specs.disk_speed ?? '—')} MB/s)`],
                ['PCIe', `gen${String(specs.pcie ?? '—')} x${String(specs.pcie_bw ?? '—')}`],
                ['CUDA', String(specs.cuda_version ?? '—')],
                ['net', `${String(specs.net_down ?? '—')}↓ / ${String(specs.net_up ?? '—')}↑ Mbps`],
                ['reliability', num(listing.reliability, 3)],
                ['on-demand', `${usd(usdPrice.usd?.on_demand_usd, 2)}/day`],
                ['spot min', `${usd(usdPrice.usd?.spot, 2)}/day`],
                ['MB', String(specs.motherboard ?? '—')],
              ]} />
            </Panel>
            <Panel title="Price history">
              <Chart option={priceOption} height={180} />
            </Panel>
            <Panel title={`Spec history (${d.specs.length})`}>
              <div className="space-y-1 text-xs">
                {d.specs.map((s, i) => (
                  <div key={i} className="flex justify-between border-b border-gray-800/50 py-1">
                    <span className="text-gray-400">
                      {dt(String(s.observed_at))}
                      {s.valid_to ? ` → ${dt(String(s.valid_to))}` : ' → now'}
                    </span>
                    <span className="text-gray-300">
                      {String(s.cpu_model ?? '').slice(0, 40)} · {String(s.cpu_offer ?? '')}
                    </span>
                  </div>
                ))}
              </div>
            </Panel>
            <Panel title={`Our rentals (${d.rentals.length})`}>
              {d.rentals.length === 0
                ? <p className="text-sm text-gray-500">never rented</p>
                : (
                  <div className="space-y-1 text-sm">
                    {d.rentals.map((r) => (
                      <div key={r.order_id} className="flex justify-between">
                        <span>#{r.order_id} <span className="text-gray-500">{r.alias}</span></span>
                        <span className="text-gray-400">
                          {r.provision_state}
                          {' · '}{typeof r.its_total === 'number' ? `${r.its_total.toFixed(0)} its` : '— its'}
                          {' · '}{typeof r.shares_hr === 'number' ? `${(r.shares_hr as number).toFixed(0)} sh/hr` : '—'}
                        </span>
                      </div>
                    ))}
                  </div>
                )}
            </Panel>
          </>
        )}
      </div>
      {rentOpen && (
        <RentModal
          serverId={serverId}
          gpu={row?.gpu ?? (((specs.gpu_array as string[]) ?? [])[0] ?? null)}
          cpu={row?.cpu ?? (specs.cpu as string | undefined) ?? null}
          priceDay={(usdPrice.usd?.on_demand_usd as number | undefined) ?? null}
          mrl={mrl}
          onClose={() => setRentOpen(false)}
          onDone={() => {
            qc.invalidateQueries({ queryKey: ['fleet'] })
            qc.invalidateQueries({ queryKey: ['orders'] })
            qc.invalidateQueries({ queryKey: ['summary'] })
          }}
        />
      )}
    </div>
  )
}
