import { useEffect, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../api'
import { Panel } from '../components/ui'

const POLL = 30_000

const inputCls =
  'w-full rounded border border-gray-700 bg-[#0d0f12] px-2 py-1 text-sm text-gray-200 outline-none focus:border-teal-500'

const LIVE_FIELDS = [
  ['upgrade_margin', 'upgrade margin'],
  ['promote_margin', 'promote margin'],
  ['max_price_hr', 'max $/hr'],
  ['min_seat_return', 'seat floor ×'],
] as const

// inert while loss_mining is checked -- rendered greyed out
const LM_FIELDS = [
  ['min_value_ratio', 'min value ratio'],
  ['min_return', 'min return'],
] as const

const GATE_FIELDS = [...LIVE_FIELDS, ...LM_FIELDS]

// integer fleet sizing; slot_min is inert under static slot_mode
const INT_FIELDS = [
  ['slot_min', 'min slots'],
  ['max_rentals', 'max slots'],
  ['gpu_trial_min', 'gpu diversity ≤'],
] as const

// dynamic-only: rig must beat its peers' mean score by 1+margin to
// count exceptional (earns a growth seat); shrink picks the policy
const FLEET_FLOAT_FIELDS = [
  ['growth_margin', 'growth margin'],
  ['seat_its_floor_frac', 'seat its floor ×'],
] as const
const SHRINK_MODES = ['passive', 'no_renew', 'cull'] as const

// dry timing knobs (minutes)
const TIMING_FIELDS = [
  ['startup_min', 'startup min'],
  ['dry_after_min', 'dry after min'],
] as const
// Poisson kill evidence (expected shares, not minutes)
const LAMBDA_FIELDS = [
  ['kill_lambda', 'kill lambda'],
  ['kill_lambda_free', 'kill lambda (free window)'],
] as const

const SPOT_MODES = ['auto', 'prefer', 'only', 'off'] as const
const SPOT_SELECTS = [
  ['spot_probe_mode', 'spot probes'],
  ['spot_slot_mode', 'spot slots'],
] as const
const SPOT_FLOAT_FIELDS = [
  ['spot_bid_premium', 'spot bid premium'],
] as const

// inert while pool_share_enabled is unchecked -- display-only
// estimator input today; the Experiment tab prices holding it
const SHARE_FIELDS = [
  ['pool_share_target_pct', 'target % of pool'],
  ['pool_share_max_price_hr', 'catch-up price cap $/hr'],
  ['pool_share_reapprove_drift', 're-approve drift'],
] as const

const TEXT_FIELDS = [
  ...GATE_FIELDS, ...INT_FIELDS, ...FLEET_FLOAT_FIELDS,
  ...TIMING_FIELDS, ...LAMBDA_FIELDS, ...SPOT_FLOAT_FIELDS,
  ...SHARE_FIELDS,
]
const INT_KEYS: Set<string> =
  new Set([...INT_FIELDS, ...TIMING_FIELDS].map(([k]) => k))
const SECTION_CLS =
  'basis-full pt-1 text-[11px] uppercase tracking-wide text-gray-600'

function PhasePanel() {
  const qc = useQueryClient()
  const ph = useQuery({ queryKey: ['phase'], queryFn: api.phaseStatus, refetchInterval: POLL })
  const [budget, setBudget] = useState('50')
  const [reserve, setReserve] = useState('0.50')
  const [hours, setHours] = useState('48')
  const [toEpoch, setToEpoch] = useState(true)
  const [armEnd, setArmEnd] = useState(false)
  const [msg, setMsg] = useState('')
  const [busy, setBusy] = useState(false)
  const d = ph.data
  const open = d?.open ?? null
  const canStart = !open || !open.active

  function invalidate() {
    for (const k of ['phase', 'config', 'experiment', 'summary'])
      qc.invalidateQueries({ queryKey: [k] })
  }

  function start() {
    const b = budget.trim(), r = reserve.trim()
    if (!b || !Number.isFinite(Number(b)) || Number(b) <= 0) {
      setMsg('budget must be a positive USD amount'); return
    }
    if (r && (!Number.isFinite(Number(r)) || Number(r) < 0)) {
      setMsg('reserve must be a USD amount >= 0 or blank'); return
    }
    if (!toEpoch && (!Number.isFinite(Number(hours)) || Number(hours) <= 0)) {
      setMsg('hours must be a positive number'); return
    }
    setBusy(true); setMsg('')
    api.phaseStart({
      budget_usd: b, reserve_usd: r || null,
      until_epoch_end: toEpoch,
      phase_hours: toEpoch ? null : Number(hours),
    }).then((p) => {
      setMsg(`phase ${p.phase_id} started → ${p.ends_at.slice(0, 16)}Z`)
      invalidate()
    }).catch((e) => setMsg(String(e.message ?? e)))
      .finally(() => setBusy(false))
  }

  function closePhase() {
    if (!armEnd) { setArmEnd(true); return }
    setBusy(true); setMsg('')
    api.phaseClose().then((r) => {
      setArmEnd(false)
      setMsg(`phase ${r.phase_id} closed`)
      invalidate()
    }).catch((e) => setMsg(String(e.message ?? e)))
      .finally(() => setBusy(false))
  }

  const fmt = (s: string) => s.replace('T', ' ').slice(5, 16)
  return (
    <Panel title="Phase"
      right={<span className="text-[11px] text-gray-500">runner binds a phase at launch</span>}>
      <div className="space-y-3 text-sm">
        {open?.active ? (
          <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
            <span>
              <b className="text-teal-300">phase {open.phase_id}</b>
              {' '}{fmt(open.started_at)} → {fmt(open.ends_at)} UTC
              {' · '}budget ${open.budget_usd} · reserve ${open.reserve_usd}
            </span>
            <button onClick={closePhase} disabled={busy}
              className={`rounded px-3 py-1 text-sm font-semibold disabled:opacity-50 ${
                armEnd ? 'bg-red-600 text-white'
                       : 'border border-gray-700 text-gray-300'}`}>
              {armEnd ? 'confirm end phase' : 'end phase now'}
            </button>
            {armEnd && (
              <button onClick={() => setArmEnd(false)}
                className="rounded border border-gray-700 px-2 py-1 text-xs text-gray-400">
                cancel
              </button>
            )}
          </div>
        ) : (
          <div className="flex flex-wrap items-end gap-x-4 gap-y-2">
            {open && !open.active && (
              <span className="pb-1.5 text-xs text-amber-400">
                phase {open.phase_id} expired {fmt(open.ends_at)} UTC (unclosed)
              </span>
            )}
            {!open && <span className="pb-1.5 text-xs text-gray-500">no open phase</span>}
            <label className="text-xs text-gray-500">
              budget USD
              <input className={`${inputCls} mt-1 w-24`} value={budget}
                onChange={(e) => setBudget(e.target.value)} inputMode="decimal" />
            </label>
            <label className="text-xs text-gray-500">
              reserve USD
              <input className={`${inputCls} mt-1 w-20`} value={reserve}
                onChange={(e) => setReserve(e.target.value)} inputMode="decimal" />
            </label>
            <label className="flex items-center gap-2 pb-1.5 text-sm"
              title={d ? `next boundary ${fmt(d.epoch_ends_at)} UTC` : undefined}>
              <input type="checkbox" checked={toEpoch}
                onChange={(e) => setToEpoch(e.target.checked)} className="accent-teal-400" />
              until epoch end{d ? ` (${fmt(d.epoch_ends_at)})` : ''}
            </label>
            {!toEpoch && (
              <label className="text-xs text-gray-500">
                hours
                <input className={`${inputCls} mt-1 w-16`} value={hours}
                  onChange={(e) => setHours(e.target.value)} inputMode="numeric" />
              </label>
            )}
            <button onClick={start} disabled={busy || !canStart}
              className="rounded bg-teal-600 px-3 py-1.5 text-sm font-semibold text-white disabled:opacity-50">
              {busy ? '…' : 'start phase'}
            </button>
          </div>
        )}
        {msg && <p className="text-xs text-amber-400">{msg}</p>}
        {(d?.recent?.length ?? 0) > 1 && (
          <div className="text-[11px] text-gray-600">
            recent: {d!.recent.slice(0, 5).map((r) =>
              `#${r.phase_id} ${fmt(r.started_at)}→${fmt(r.ends_at)} $${r.budget_usd}` +
              (r.closed_at ? ' (closed)' : r.active ? ' (active)' : ' (expired)')
            ).join(' · ')}
          </div>
        )}
        <p className="text-[11px] text-gray-600">
          ending marks the phase closed and pauses its live config next cycle;
          live rentals run to term — boundaries never cancel. a running loop
          keeps the phase it launched with, so restart the runner to run a
          new phase.
        </p>
      </div>
    </Panel>
  )
}

export default function Settings() {
  const qc = useQueryClient()
  const cfg = useQuery({ queryKey: ['config'], queryFn: api.config, refetchInterval: POLL })
  const [score, setScore] = useState('net')
  const [gates, setGates] = useState<Record<string, string>>({})
  const [loss, setLoss] = useState(false)
  const [paused, setPaused] = useState(false)
  const [probes, setProbes] = useState(true)
  const [slotMode, setSlotMode] = useState('static')
  const [shrinkMode, setShrinkMode] = useState('passive')
  const [spotOn, setSpotOn] = useState(false)
  const [shareOn, setShareOn] = useState(false)
  const [spotModes, setSpotModes] = useState<Record<string, string>>({})
  const [msg, setMsg] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    const c = cfg.data
    if (!c) return
    setScore(c.score || 'net')
    setGates(Object.fromEntries(TEXT_FIELDS.map(([k]) => [k, String(c[k] ?? '')])))
    setLoss(Boolean(c.loss_mining))
    setPaused(Boolean(c.paused))
    setProbes(c.probes_enabled !== false)
    setSlotMode(c.slot_mode || 'static')
    setShrinkMode(c.shrink_mode || 'passive')
    setSpotOn(Boolean(c.spot_enabled))
    setShareOn(Boolean(c.pool_share_enabled))
    setSpotModes(Object.fromEntries(SPOT_SELECTS.map(([k]) => [k, c[k] || 'auto'])))
  }, [cfg.data])

  function save() {
    const body: Record<string, unknown> = {
      score, loss_mining: loss, paused, probes_enabled: probes,
      slot_mode: slotMode, shrink_mode: shrinkMode,
      spot_enabled: spotOn, pool_share_enabled: shareOn,
    }
    for (const [k] of TEXT_FIELDS) {
      if (loss && LM_FIELDS.some(([g]) => g === k)) continue
      if (slotMode === 'static'
          && (k === 'slot_min' || k === 'growth_margin')) continue
      if (!spotOn && SPOT_FLOAT_FIELDS.some(([g]) => g === k)) continue
      if (!shareOn && SHARE_FIELDS.some(([g]) => g === k)) continue
      const raw = (gates[k] ?? '').trim()
      if (raw === '') continue
      if (k === 'max_price_hr' || k === 'pool_share_max_price_hr') {
        body[k] = raw
      } else {
        const n = Number(raw)
        if (!Number.isFinite(n) || (INT_KEYS.has(k) && !Number.isInteger(n))) {
          setMsg(`${k} must be ${INT_KEYS.has(k) ? 'a whole number' : 'a number'} or blank`)
          return
        }
        body[k] = n
      }
    }
    if (spotOn) {
      for (const [k] of SPOT_SELECTS) body[k] = spotModes[k] || 'auto'
    }
    setBusy(true)
    setMsg('')
    api.updateConfig(body).then(() => {
      setMsg('saved')
      qc.invalidateQueries({ queryKey: ['config'] })
      qc.invalidateQueries({ queryKey: ['experiment'] })
    }).catch((e) => setMsg(String(e.message ?? e)))
      .finally(() => setBusy(false))
  }

  if (!cfg.data) return null
  const field = ([k, lbl]: readonly [string, string], dis = false) => (
    <label key={k} className={`text-xs text-gray-500 ${dis ? 'opacity-40' : ''}`}>
      {lbl}
      <input className={`${inputCls} mt-1 w-24`} value={gates[k] ?? ''}
        onChange={(e) => setGates((g) => ({ ...g, [k]: e.target.value }))}
        inputMode="decimal" disabled={dis} />
    </label>
  )
  return (
    <div className="space-y-3">
    <PhasePanel />
    <Panel title="Strategy"
      right={<span className="text-[11px] text-gray-500">live config, runner re-reads each cycle</span>}>
      <div className="flex flex-wrap items-end gap-x-4 gap-y-2 text-sm">
        <label className="text-xs text-gray-500">
          score
          <select className={`${inputCls} mt-1 w-28`} value={score}
            onChange={(e) => setScore(e.target.value)}>
            {['net', 'gross', 'its'].map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
          <div className="mt-1 max-w-[16rem] text-[11px] text-gray-500">
            {cfg.data.score_help?.[score]}
          </div>
        </label>
        {LIVE_FIELDS.map(([k, lbl]) => field([k, lbl] as const))}
        <label className="flex items-center gap-2 pb-1.5 text-sm">
          <input type="checkbox" checked={loss}
            onChange={(e) => setLoss(e.target.checked)} className="accent-teal-400" />
          loss_mining
        </label>
        {LM_FIELDS.map(([k, lbl]) => field([k, lbl] as const, loss))}
        <label className="flex items-center gap-2 pb-1.5 text-sm">
          <input type="checkbox" checked={paused}
            onChange={(e) => setPaused(e.target.checked)} className="accent-teal-400" />
          paused
        </label>
        <label className="flex items-center gap-2 pb-1.5 text-sm"
          title="off = slots cook, vacancies fill direct, no probe spend">
          <input type="checkbox" checked={probes}
            onChange={(e) => setProbes(e.target.checked)} className="accent-teal-400" />
          probes_enabled
        </label>
        <span className={SECTION_CLS}>fleet</span>
        <label className="text-xs text-gray-500">
          slot_mode
          <select className={`${inputCls} mt-1 w-28`} value={slotMode}
            onChange={(e) => setSlotMode(e.target.value)}>
            {['dynamic', 'static'].map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
        </label>
        {field(INT_FIELDS[0] as readonly [string, string], slotMode === 'static')}
        {field(INT_FIELDS[1] as readonly [string, string])}
        {field(INT_FIELDS[2] as readonly [string, string])}
        {FLEET_FLOAT_FIELDS.map(([k, lbl]) => field(
          [k, lbl] as const, slotMode === 'static'))}
        <label className={`text-xs text-gray-500 ${slotMode === 'static' ? 'opacity-40' : ''}`}
          title="over-target policy: passive = attrition only, no_renew = weakest excess exits at term end, cull = cancel weakest below-average excess each cycle">
          shrink_mode
          <select className={`${inputCls} mt-1 w-28`} value={shrinkMode}
            onChange={(e) => setShrinkMode(e.target.value)}
            disabled={slotMode === 'static'}>
            {SHRINK_MODES.map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
        </label>
        <span className={SECTION_CLS}>timing</span>
        {TIMING_FIELDS.map(([k, lbl]) => field([k, lbl] as const))}
        {LAMBDA_FIELDS.map(([k, lbl]) => field([k, lbl] as const))}
        <span className={SECTION_CLS}>spot</span>
        <label className="flex items-center gap-2 pb-1.5 text-sm">
          <input type="checkbox" checked={spotOn}
            onChange={(e) => setSpotOn(e.target.checked)} className="accent-teal-400" />
          spot_enabled
        </label>
        {SPOT_SELECTS.map(([k, lbl]) => (
          <label key={k} className={`text-xs text-gray-500 ${!spotOn ? 'opacity-40' : ''}`}>
            {lbl}
            <select className={`${inputCls} mt-1 w-24`} value={spotModes[k] || 'auto'}
              onChange={(e) => setSpotModes((m) => ({ ...m, [k]: e.target.value }))}
              disabled={!spotOn}>
              {SPOT_MODES.map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
          </label>
        ))}
        {SPOT_FLOAT_FIELDS.map(([k, lbl]) => field([k, lbl] as const, !spotOn))}
        <span className={SECTION_CLS}>pool share</span>
        <label className="flex items-center gap-2 pb-1.5 text-sm"
          title="declares a pool-window share objective -- the Experiment tab shows the live budget estimate (estimator only, no spending)">
          <input type="checkbox" checked={shareOn}
            onChange={(e) => setShareOn(e.target.checked)} className="accent-teal-400" />
          pool_share_enabled
        </label>
        {SHARE_FIELDS.map(([k, lbl]) => field([k, lbl] as const, !shareOn))}
        <button onClick={save} disabled={busy}
          className="rounded bg-teal-600 px-3 py-1.5 text-sm font-semibold text-white disabled:opacity-50">
          {busy ? 'saving…' : 'Save'}
        </button>
        {msg && <span className="pb-1.5 text-xs text-amber-400">{msg}</span>}
      </div>
    </Panel>
    </div>
  )
}
