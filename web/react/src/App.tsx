import { useQuery } from '@tanstack/react-query'
import type { ComponentType } from 'react'
import { useState } from 'react'
import { api } from './api'
import { ago } from './lib/format'
import Candidates from './views/Candidates'
import Experiment from './views/Experiment'
import Fleet from './views/Fleet'
import Marketplace from './views/Marketplace'
import Orders from './views/Orders'
import Overview from './views/Overview'
import Pool from './views/Pool'
import Settings from './views/Settings'

const TABS = [
  { id: 'overview', label: 'Overview' },
  { id: 'candidates', label: 'Candidates' },
  { id: 'marketplace', label: 'Marketplace' },
  { id: 'fleet', label: 'Fleet' },
  { id: 'orders', label: 'Orders' },
  { id: 'experiment', label: 'Experiment' },
  { id: 'pool', label: 'Pool' },
  { id: 'settings', label: 'Settings' },
] as const

type TabId = (typeof TABS)[number]['id']

const VIEWS: Record<TabId, ComponentType> = {
  overview: Overview,
  candidates: Candidates,
  marketplace: Marketplace,
  fleet: Fleet,
  orders: Orders,
  experiment: Experiment,
  pool: Pool,
  settings: Settings,
}

export default function App() {
  const [tab, setTab] = useState<TabId>('overview')
  const summary = useQuery({
    queryKey: ['summary'], queryFn: api.summary, refetchInterval: 30_000,
  })
  const View = VIEWS[tab]

  return (
    <div className="flex min-h-screen flex-col bg-[#16181c] font-sans text-gray-200">
      <header className="flex items-center gap-4 border-b border-gray-800 bg-[#1e2228] px-4 py-2">
        <h1 className="text-lg font-bold text-teal-400">clorqub</h1>
        <nav className="flex flex-wrap gap-1">
          {TABS.map((t) => (
            <button
              key={t.id}
              onClick={() => setTab(t.id)}
              className={`rounded px-3 py-1 text-sm transition-colors ${
                tab === t.id
                  ? 'bg-teal-500/15 font-semibold text-teal-300'
                  : 'text-gray-400 hover:bg-gray-700/40 hover:text-gray-200'
              }`}
            >
              {t.label}
            </button>
          ))}
        </nav>
        <div className="ml-auto flex items-center gap-3 text-xs text-gray-500">
          {summary.data?.experiment.runner_alive && (
            <span className="rounded bg-teal-500/15 px-1.5 py-0.5 text-teal-300">exp</span>
          )}
          <span>snapshot {ago(summary.data?.captured_at)}</span>
        </div>
      </header>
      <main className="flex-1 p-3">
        <View />
      </main>
    </div>
  )
}
