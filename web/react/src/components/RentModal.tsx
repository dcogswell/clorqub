import { useEffect, useRef, useState } from 'react'
import { api } from '../api'
import type { RentReview, RentStatus } from '../api'
import { KV } from './ui'
import { num, usd } from '../lib/format'

type Step = 'form' | 'quoting' | 'review' | 'running' | 'done'

const FALLBACK_MAX_HOURS = 24

export function Modal({ title, onClose, children, width = 480 }: {
  title: string
  onClose: () => void
  children: React.ReactNode
  width?: number
}) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60"
      onClick={onClose}>
      <div className="max-h-[85vh] w-full overflow-hidden rounded-xl border border-gray-700 bg-[#1a1d22]"
        style={{ maxWidth: width }}
        onClick={(e) => e.stopPropagation()}>
        <header className="flex items-center justify-between border-b border-gray-800 px-4 py-2.5">
          <h2 className="text-base font-bold text-teal-300">{title}</h2>
          <button onClick={onClose}
            className="rounded px-2 py-0.5 text-gray-400 hover:bg-gray-800">✕</button>
        </header>
        <div className="max-h-[75vh] overflow-y-auto p-4">{children}</div>
      </div>
    </div>
  )
}

const inputCls =
  'w-full rounded border border-gray-700 bg-[#0d0f12] px-2 py-1.5 text-sm text-gray-200 outline-none focus:border-teal-500'

export function RentModal({ serverId, gpu, cpu, priceDay, mrl, onClose, onDone }: {
  serverId: number
  gpu: string | null
  cpu: string | null
  priceDay: number | null
  mrl: number | null
  onClose: () => void
  onDone: () => void
}) {
  const [step, setStep] = useState<Step>('form')
  const [mode, setMode] = useState<'on-demand' | 'spot'>('on-demand')
  const [hours, setHours] = useState('1')
  const [cap, setCap] = useState('')
  const [pool, setPool] = useState('jetski')
  const [threads, setThreads] = useState('')
  const [renew, setRenew] = useState(false)
  const [premium, setPremium] = useState('')
  const [formError, setFormError] = useState('')
  const [token, setToken] = useState<string | null>(null)
  const [review, setReview] = useState<RentReview | null>(null)
  const [lines, setLines] = useState<string[]>([])
  const [result, setResult] = useState<RentStatus | null>(null)
  const logRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    logRef.current?.scrollTo(0, logRef.current.scrollHeight)
  }, [lines])

  useEffect(() => {
    if (step !== 'running' || !token) return
    const t = setInterval(async () => {
      try {
        const st = await api.rentStatus(token)
        setLines(st.lines)
        if (st.done) {
          setResult(st)
          setStep('done')
          clearInterval(t)
          onDone()
        }
      } catch { /* keep polling */ }
    }, 1500)
    return () => clearInterval(t)
  }, [step, token, onDone])

  const maxHours = mrl ?? FALLBACK_MAX_HOURS

  function submitForm() {
    const h = parseInt(hours, 10)
    if (!Number.isInteger(h) || h < 1 || h > maxHours) {
      setFormError(`hours must be an integer 1..${maxHours}`)
      return
    }
    const capText = cap.trim()
    if (capText && (!Number.isFinite(Number(capText)) || Number(capText) <= 0)) {
      setFormError('cost ceiling must be a positive USD amount or blank')
      return
    }
    const thText = threads.trim()
    let th: number | null = null
    if (thText) {
      th = parseInt(thText, 10)
      if (!Number.isInteger(th) || th < 1 || th > 512) {
        setFormError('cpu threads must be an integer 1..512 or blank')
        return
      }
    }
    let prem: number | null = null
    const premText = premium.trim()
    if (premText) {
      prem = Number(premText)
      if (!Number.isFinite(prem) || prem < 1.0 || prem > 1.5) {
        setFormError('bid premium must be 1.00–1.50 or blank (default 1.01)')
        return
      }
    }
    setFormError('')
    setStep('quoting')
    api.rentQuote({
      server_id: serverId, hours: h, pool,
      max_cost_usd: capText || null,
      cpu_threads: th, auto_renew: renew,
      mode, spot_bid_premium: prem,
    }).then((res) => {
      setToken(res.token)
      setReview(res.review)
      setLines(res.lines)
      setStep('review')
    }).catch((e) => {
      setFormError(String(e.message ?? e))
      setStep('form')
    })
  }

  function confirm() {
    if (!token) return
    setStep('running')
    api.rentConfirm(token).catch((e) => {
      setLines((l) => [...l, `confirm failed: ${e.message ?? e}`])
      setResult({ lines: [], done: true, rc: 1, info: null })
      setStep('done')
    })
  }

  function cancelQuote() {
    if (token) api.rentCancelQuote(token).catch(() => {})
    onClose()
  }

  const mrlHint = mrl ? `host max ${mrl}h` : 'host max unknown'
  const busy = step === 'quoting' || step === 'running'

  return (
    <Modal title={`rent server ${serverId}`} onClose={busy ? () => {} : onClose} width={520}>
      {step === 'form' && (
        <div className="space-y-3 text-sm">
          <div className="rounded bg-[#0d0f12] p-2 text-xs text-gray-400">
            GPU {gpu ?? '—'} · CPU {cpu ?? '—'} · {usd(priceDay, 2)}/day cached.
            {mode === 'spot'
              ? 'Spot: no fixed term — runs until outbid or cancelled. Bid is fetched live at review and re-checked before the order posts.'
              : 'On-demand: fixed term. '}Price/fees re-fetched live for the review.
            Nothing spends until Confirm &amp; Rent.
          </div>
          <div>
            <label className="text-xs text-gray-500">Type</label>
            <select className={`${inputCls} mt-1`} value={mode}
              onChange={(e) => setMode(e.target.value as 'on-demand' | 'spot')}>
              <option value="on-demand">on-demand (fixed term)</option>
              <option value="spot">spot (billed/min until outbid)</option>
            </select>
          </div>
          <div>
            <label className="text-xs text-gray-500">
              {mode === 'spot'
                ? `Budget-horizon hours (1–${maxHours}; bounds the spend cap only — no term)`
                : `Hours (1–${maxHours}; ${mrlHint})`}
            </label>
            <div className="mt-1 flex gap-2">
              <input className={inputCls} value={hours}
                onChange={(e) => setHours(e.target.value)} inputMode="numeric" />
              {mode === 'on-demand' && mrl != null && (
                <button onClick={() => setHours(String(mrl))}
                  className="shrink-0 rounded border border-gray-700 px-2 text-xs text-teal-300">
                  Max
                </button>
              )}
            </div>
          </div>
          {mode === 'spot' && (
            <div>
              <label className="text-xs text-gray-500">
                Bid premium × top bid (blank = 1.01, min Clore requires)
              </label>
              <input className={`${inputCls} mt-1`} value={premium}
                placeholder="1.01"
                onChange={(e) => setPremium(e.target.value)} inputMode="decimal" />
            </div>
          )}
          {mode === 'on-demand' && (
            <label className="flex items-center gap-2 text-sm">
              <input type="checkbox" checked={renew} onChange={(e) => setRenew(e.target.checked)}
                className="accent-teal-400" />
              auto-renew (keeps billing each term until you cancel)
            </label>
          )}
          <div>
            <label className="text-xs text-gray-500">Cost ceiling USD (blank = confirm exact quote)</label>
            <input className={`${inputCls} mt-1`} value={cap} placeholder="blank = exact quote"
              onChange={(e) => setCap(e.target.value)} inputMode="decimal" />
          </div>
          <div>
            <label className="text-xs text-gray-500">Pool</label>
            <select className={`${inputCls} mt-1`} value={pool}
              onChange={(e) => setPool(e.target.value)}>
              <option value="jetski">jetski</option>
              <option value="qli">qli</option>
            </select>
          </div>
          <div>
            <label className="text-xs text-gray-500">CPU threads (blank = listing)</label>
            <input className={`${inputCls} mt-1`} value={threads}
              placeholder="cpu threads (blank = listing)"
              onChange={(e) => setThreads(e.target.value)} inputMode="numeric" />
          </div>
          {formError && <p className="text-sm text-red-400">{formError}</p>}
          <div className="flex justify-end gap-2 pt-1">
            <button onClick={onClose}
              className="rounded border border-gray-700 px-3 py-1.5 text-sm text-gray-300">
              Cancel
            </button>
            <button onClick={submitForm}
              className="rounded bg-teal-600 px-3 py-1.5 text-sm font-semibold text-white">
              Review &amp; Rent
            </button>
          </div>
        </div>
      )}

      {step === 'quoting' && (
        <div className="space-y-2 text-sm">
          <p className="text-teal-300">fetching fresh quote — live API checks…</p>
          <LogLines logRef={logRef} lines={lines} />
        </div>
      )}

      {step === 'review' && review && (
        <div className="space-y-3 text-sm">
          <p className="font-semibold text-amber-400">REVIEW ORDER — nothing placed yet</p>
          {review.blacklist_warning && (
            <p className="rounded bg-amber-500/15 p-2 text-amber-400">
              ⚠ {review.blacklist_warning}
            </p>
          )}
          <KV items={[
            ['server', review.server_id],
            ['gpu', review.gpu],
            ['cpu', review.cpu],
            ['threads', `listed ${review.listed_threads} → chosen ${review.chosen_threads ?? '—'}`],
            ['pool', `${review.pool}   alias ${review.alias}`],
            ['type', review.mode === 'spot'
              ? 'spot — no fixed term, billed/min at bid until outbid or cancelled'
              : 'on-demand'],
            ...(review.mode === 'spot'
              ? [
                ['spot bid', `$${review.spot_bid_usd_day}/day (floor $${review.spot_floor_usd_day ?? '?'}, top competing ${review.spot_top_bid_usd_day != null ? `$${review.spot_top_bid_usd_day}` : 'none'}, ${review.spot_offers ?? 0} offers)`],
                ['budget horizon', `${num(review.hours)}h — sizes the spend cap only`],
              ] as [string, React.ReactNode][]
              : [
                ['hours', `${num(review.hours)} ${review.auto_renew ? 'per term, auto-renew ON' : ' auto-expires, renewal off'}`],
              ] as [string, React.ReactNode][]),
            ['cost bound', `${usd(review.cost_bound_usd, 2)} incl live fees (hourly ${usd(review.hourly_cost_usd, 4)}, creation ${usd(review.creation_fee_usd, 2)})`],
            ['ceiling', review.ceiling ? `$${review.ceiling}` : 'none — you confirm the exact bound'],
            ['ssh key', review.key_path],
          ]} />
          {review.mode === 'spot' && (
            <p className="text-xs text-amber-400">
              spot has no term protection — the server leaves the moment a
              higher bid lands; the bid is re-checked against the live market
              once more before the order posts.
            </p>
          )}
          {review.mode !== 'spot' && review.auto_renew && (
            <p className="text-xs text-amber-400">
              auto-renew ON — re-bills ~{usd(review.cost_bound_usd, 2)} every {num(review.hours)}h
              term until cancelled (bound covers ONE term only)
            </p>
          )}
          <p className="text-xs text-gray-500">
            terms: auto-cancel if the receipt lacks the bounded term, or if provisioning fails.
            risk: the host can technically read rig memory; credentials are pushed to the rig.
          </p>
          <div className="flex justify-end gap-2">
            <button onClick={cancelQuote}
              className="rounded border border-gray-700 px-3 py-1.5 text-sm text-gray-300">
              Cancel
            </button>
            <button onClick={confirm}
              className="rounded bg-teal-600 px-3 py-1.5 text-sm font-semibold text-white">
              Confirm &amp; Rent
            </button>
          </div>
        </div>
      )}

      {(step === 'running' || step === 'done') && (
        <div className="space-y-2 text-sm">
          {step === 'running'
            ? <p className="text-teal-300">creating order — one POST, no retries…</p>
            : <p className={result?.rc === 0 ? 'font-semibold text-teal-300' : 'font-semibold text-amber-400'}>
                {result?.rc === 0
                  ? `done: order ${result.info?.order_id ?? '?'} READY`
                  : `finished rc=${result?.rc} stage=${result?.info?.stage ?? '-'}`}
              </p>}
          <LogLines logRef={logRef} lines={lines} />
          {step === 'done' && (
            <div className="flex justify-end">
              <button onClick={onClose}
                className="rounded bg-teal-600 px-3 py-1.5 text-sm font-semibold text-white">
                Close
              </button>
            </div>
          )}
        </div>
      )}
    </Modal>
  )
}

function LogLines({ lines, logRef }: {
  lines: string[]
  logRef: React.RefObject<HTMLDivElement | null>
}) {
  return (
    <div ref={logRef}
      className="max-h-48 overflow-y-auto rounded bg-[#0d0f12] p-2 font-mono text-[11px] leading-4 text-gray-400">
      {lines.map((l, i) => <div key={i}>{l}</div>)}
      {lines.length === 0 && <span className="text-gray-600">…</span>}
    </div>
  )
}
