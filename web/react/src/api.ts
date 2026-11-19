export interface Summary {
  captured_at: string | null
  source: string | null
  total: number
  available: number
  ranked: number
  balance_usd: number | null
  balance_at: string | null
  fleet_active: number
  orders_active: number
  shares_hr_total: number | null
  experiment: {
    runner_alive: boolean | null
    phase: Record<string, unknown> | null
    n_events: number
    n_candidates: number
  }
}

export interface MarketRow {
  server_id: number
  owner: number | null
  country: string | null
  gpu: string | null
  cpu: string | null
  cpu_isa: string | null
  threads_listed: string | null
  cuda: string | null
  reliability: number | null
  price_day_usd: number | null
  hourly_cost_usd: number | null
  estimated_sols_hr: number | null
  estimated_sols_per_usd: number | null
  est_its: number | null
  est_its_per_usd: number | null
  eligible: boolean
  ineligible_reason: string | null
  benchmarked: boolean
  available: boolean
  prior_rentals?: number
  blacklist?: {
    reason: string
    source: string
    added_at: string
    expires_at: string | null
    detail: Record<string, unknown>
  } | null
  spot: Record<string, unknown> | null
  mode?: string | null
  od_hourly?: number | null
  spot_hourly?: number | null
  spot_discount?: number | null
  [k: string]: unknown
}

export interface GpuStat {
  prior: number | null
  proven: number
  rentals: number
  shares: number
  weight?: number | null
  weight_n?: number
  sol?: number
  cross_epoch: boolean
  median_hr?: number | null
  listings?: number
}

export interface Candidate {
  server_id: number
  gpu: string | null
  cpu: string | null
  meas_its: number | null
  meas_rentals: number
  meas_cross_epoch: boolean
  est_its: number | null
  extrapolated: boolean
  usd_hr: number | null
  return_x: number | null
  net_hr: number | null
  prior: number | null
  eff_hr: number | null
  hourly: number | null
  fill_status: string | null
  upgrade_status: string | null
  mode?: string | null
  od_hourly?: number | null
  spot_hourly?: number | null
  spot_discount?: number | null
}

export interface Rig {
  order_id: number
  server_id: number
  alias: string
  pool: string
  created_at: string
  deadline_at: string | null
  provision_state: string
  visible?: boolean
  health: string | null
  auto_renew: number | null
  attempted_at: string | null
  observed_at: string | null
  error_kind: string | null
  max_cost_usd: string | null
  quoted_cost_usd: string | null
  control: string | null
  mode?: string | null
  its_total?: number | null
  last_share_at?: string | null
  rate_hr_usd: number | null
  spend_usd: number | null
  price_day_native: number | null
  up_until: string | null
  status: {
    its?: { cpu?: number | null; gpu?: number | null }
    shares?: { found?: number; submitted?: number; invalid?: number; epoch?: number }
    hardware?: { cpu_model?: string; nproc?: number; avx2?: boolean; avx512f?: boolean; gpus?: string[] }
    miner?: { running?: boolean }
    package?: { version?: string }
    pool?: string
  } | null
  [k: string]: unknown
}

export interface RigPoint {
  ts: string
  error: string | null
  cpu_its?: number | null
  gpu_its?: number | null
  found?: number | null
  submitted?: number | null
  invalid?: number | null
  epoch?: number | null
  miner_running?: boolean | null
  pool?: string | null
}

export interface Order {
  order_id: number
  server_id: number | null
  state: string
  cpu: string | null
  gpus: string[] | null
  price_day_native: number | null
  quoted_creation_fee_native: number | null
  spend_reported_native: number | null
  shares_hr: number | null
  shares_per_cent: number | null
  online_reported: boolean | null
  first_seen: string
  last_seen: string
  [k: string]: unknown
}

export interface ExpEvent {
  id: number
  ts: string
  kind: string
  order_id: number | null
  server_id: number | null
  detail: Record<string, unknown>
}

export interface Verdict {
  order_id: number
  server_id: number
  role: string | null
  mode: string | null
  tier: string | null
  ended_at: string
  verdict: string | null
  decided: string | null
  its: number | null
  shares: number | null
  age_min: number | null
  cost_usd: number
  outcome: string | null
}

export interface ExpRental {
  order_id: number | null
  server_id: number | null
  role: string | null
  mode?: string | null
  tier: string | null
  cpu: string | null
  gpu: string | null
  hourly_usd: unknown
  its_total: number | null
  forecast_return_x: number | null
  forecast_usd_hr: number | null
  forecast_net_hr: number | null
  net_source: string | null
  age_s: number | null
  t0: string | null
  last_share_at: string | null
  shares: number | null
  tree_id: string | null
  tree_wait?: boolean
  next: string | null
}

export interface TreeAgent {
  ts: string
  source: string
  epoch: number | null
  tree_id: string
  seed: string | null
  its: number | null
  shares: number | null
}

export interface Experiment {
  runner_alive: boolean | null
  phase: Record<string, unknown> | null
  events: ExpEvent[]
  rentals: ExpRental[]
  candidate_values: Candidate[]
  pending_bursts: unknown[]
  pending_rerents: unknown[]
  burst: Record<string, unknown> | null
  bursts: Record<string, unknown>[]
  candidate_term_hours: number | null
}

export interface ExperimentStatus {
  runner_alive: boolean | null
  phase: Record<string, unknown> | null
  pending_bursts: unknown[]
  pending_rerents: unknown[]
  burst: Record<string, unknown> | null
  bursts: Record<string, unknown>[]
  agent?: TreeAgent | null
  offering_dead?: boolean
  dead_trees?: string[]
  tree_lifetime_min?: number | null
}

export interface PoolPoint {
  source_ts: string
  epoch: number | null
  shares: number | null
  solutions: number | null
  counted: number | null
  est_gains_usd: number | null
  est_qus: number | null
  usd_per_counted: number | null
  qubic_price_usd: number | null
  pool_sol_per_hour: number | null
}

export interface PoolWorker {
  alias: string
  sol: number | null
  its: number | null
  trainer: string | null
  cuda: number | null
  avx512: number | null
  avx2: number | null
  last_seen: string | null
}

export interface ServerDetail {
  server_id: number
  owner: number | null
  country: string | null
  first_seen: string
  last_seen: string
  listing: Record<string, unknown>
  specs: Record<string, unknown>[]
  prices: { observed_at: string; valid_to: string | null; on_demand_usd_day: number | null; price_json: string }[]
  spot: Record<string, unknown> | null
  rentals: Rig[]
}

export interface ExpConfig {
  score: string
  upgrade_margin: number | null
  promote_margin: number | null
  min_value_ratio: number | null
  min_return: number | null
  min_seat_return: number | null
  gpu_trial_min: number | null
  max_price_hr: string | null
  loss_mining: boolean | null
  paused: boolean | null
  probes_enabled: boolean | null
  slot_mode: string | null
  slot_min: number | null
  max_rentals: number | null
  growth_margin: number | null
  shrink_mode: string | null
  startup_min: number | null
  dry_after_min: number | null
  kill_lambda: number | null
  kill_lambda_free: number | null
  spot_enabled: boolean | null
  spot_probe_mode: string | null
  spot_slot_mode: string | null
  spot_bid_premium: number | null
  score_help?: Record<string, string>
  strategies?: string[]
}

export interface ConfigUpdate {
  score?: string
  upgrade_margin?: number
  promote_margin?: number
  min_value_ratio?: number
  min_return?: number
  min_seat_return?: number
  gpu_trial_min?: number
  max_price_hr?: string
  loss_mining?: boolean
  paused?: boolean
  probes_enabled?: boolean
  slot_mode?: string
  slot_min?: number
  max_rentals?: number
  growth_margin?: number
  shrink_mode?: string
  startup_min?: number
  dry_after_min?: number
  kill_lambda?: number
  kill_lambda_free?: number
  spot_enabled?: boolean
  spot_probe_mode?: string
  spot_slot_mode?: string
  spot_bid_premium?: number
  strategy?: string
}

export interface RentQuoteRequest {
  server_id: number
  hours: number
  max_cost_usd?: string | null
  pool: string
  cpu_threads?: number | null
  auto_renew?: boolean
  mode?: 'on-demand' | 'spot'
  spot_bid_premium?: number | null
}

export interface RentReview {
  server_id: number
  alias: string
  pool: string
  gpu: string
  cpu: string
  listed_threads: string
  chosen_threads: number | null
  hours: number | null
  auto_renew: boolean
  cost_bound_usd: unknown
  hourly_cost_usd: unknown
  creation_fee_usd: unknown
  ceiling: string | null
  key_path: string
  mode: string
  spot_bid_usd_day: string | null
  spot_floor_usd_day: string | null
  spot_top_bid_usd_day: number | null
  spot_offers: number | null
  blacklist_warning: string | null
}

export interface RentStatus {
  lines: string[]
  done: boolean
  rc: number | null
  info: { order_id?: number | null; provision_state?: string | null; stage?: string } | null
}

export interface PhaseInfo {
  phase_id: number
  started_at: string
  ends_at: string
  budget_usd: string
  reserve_usd: string
  closed_at: string | null
  active: boolean
}

export interface PhaseStartRequest {
  budget_usd: string
  phase_hours?: number | null
  until_epoch_end?: boolean
  reserve_usd?: string | null
}

async function get<T>(url: string): Promise<T> {
  const res = await fetch(url)
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`)
  return res.json()
}

async function post<T>(url: string, body: unknown): Promise<T> {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  const text = await res.text()
  if (!res.ok) throw new Error(text || `${res.status} ${res.statusText}`)
  return JSON.parse(text)
}

export const api = {
  summary: () => get<Summary>('/api/summary'),
  market: () => get<MarketRow[]>('/api/market'),
  candidates: () =>
    get<{
      candidate_values: Candidate[]
      term_hours: number | null
      value_model: unknown
      gpu_stats: Record<string, GpuStat>
    }>('/api/candidates'),
  server: (id: number) => get<ServerDetail>(`/api/server/${id}`),
  fleet: () => get<Rig[]>('/api/fleet'),
  rigSeries: (id: number) => get<{ points: RigPoint[] }>(`/api/rig/${id}/series`),
  rigLog: (id: number, pool = 'jetski', head = false) =>
    get<{ text: string; last_captured_at: string | null }>(
      `/api/rig/${id}/log?pool=${pool}${head ? '&head=1' : ''}`),
  rigSpend: (id: number) =>
    get<{ points: { ts: string; spend: number | null; price_day: number | null }[] }>(
      `/api/rig/${id}/spend`),
  orders: () => get<Order[]>('/api/orders'),
  experiment: () => get<Experiment>('/api/experiment'),
  experimentStatus: () => get<ExperimentStatus>('/api/experiment/status'),
  experimentRentals: () => get<{ rentals: ExpRental[] }>('/api/experiment/rentals'),
  experimentEvents: () => get<{ events: ExpEvent[] }>('/api/experiment/events'),
  experimentVerdicts: () => get<{ verdicts: Verdict[] }>('/api/experiment/verdicts'),
  poolSeries: () => get<{ points: PoolPoint[] }>('/api/pool/series'),
  poolWorkers: () => get<{ points: PoolWorker[] }>('/api/pool/workers'),
  blacklist: () => get<Record<string, unknown>[]>('/api/blacklist'),
  config: () => get<ExpConfig>('/api/config'),
  updateConfig: (body: ConfigUpdate) => post<ExpConfig>('/api/config', body),
  rentQuote: (body: RentQuoteRequest) =>
    post<{ token: string; review: RentReview; lines: string[] }>('/api/rent/quote', body),
  rentConfirm: (token: string) =>
    post<{ started: boolean }>('/api/rent/confirm', { token }),
  rentCancelQuote: (token: string) =>
    post<{ dropped: boolean }>('/api/rent/cancel-quote', { token }),
  rentStatus: (token: string) => get<RentStatus>(`/api/rent/status/${token}`),
  cancelOrder: (orderId: number) =>
    post<{ rc: number; info: Record<string, unknown>; lines: string[] }>(
      '/api/orders/cancel', { order_id: orderId }),
  phaseStatus: () =>
    get<{ open: PhaseInfo | null; recent: PhaseInfo[]; epoch_ends_at: string }>(
      '/api/phase'),
  phaseStart: (body: PhaseStartRequest) =>
    post<{ phase_id: number; started_at: string; ends_at: string }>(
      '/api/phase/start', body),
  phaseClose: () =>
    post<{ closed: boolean; phase_id: number }>('/api/phase/close', {}),
  probePins: () =>
    get<{ pins: { server_id: number; tagged_at: string }[]; phase_id: number | null }>(
      '/api/probe-pins'),
  setProbePin: (serverId: number, pinned: boolean) =>
    post<{ pins: number[] }>('/api/probe-pins', { server_id: serverId, pinned }),
  removeBlacklist: (serverId: number) =>
    post<{ removed: number }>('/api/blacklist/remove', { server_id: serverId }),
}
