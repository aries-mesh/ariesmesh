import { Brain, Check, Cpu, X, Zap } from 'lucide-react'
import useAPI from '../hooks/useAPI'
import StatusBadge from '../components/StatusBadge'

function configToneBadge(type) {
  if (type === 'local') return <StatusBadge tone="success" label="local" />
  if (type === 'distributed') return <StatusBadge tone="info" label="distributed" pulse />
  if (type === 'cloud') return <StatusBadge tone="warning" label="cloud" />
  return <StatusBadge tone="muted" label={type} />
}

function ScoreBar({ value, label }) {
  const pct = Math.max(0, Math.min(100, (value || 0) * 100))
  return (
    <div className="flex items-center gap-2 text-xs">
      <span className="text-aries-text-dim w-16">{label}</span>
      <div className="flex-1 h-1 rounded-full bg-aries-border/60">
        <div
          className="h-full rounded-full bg-aries-teal/80"
          style={{ width: `${pct}%` }}
        />
      </div>
      <span className="text-aries-text font-mono w-9 text-right">{pct.toFixed(0)}</span>
    </div>
  )
}

function ConfigsTable({ configs }) {
  if (!configs || configs.length === 0) {
    return (
      <div className="rounded-(--radius-card) border border-aries-border bg-aries-surface p-8 text-center">
        <Brain size={32} strokeWidth={1.5} className="mx-auto text-aries-text-dim mb-3" />
        <div className="text-aries-text font-medium mb-1">No inference configurations yet</div>
        <div className="text-sm text-aries-text-dim max-w-md mx-auto">
          The registry didn't find a usable llama.cpp build or any GGUF model files
          on this device. Install <code className="text-aries-teal">llama-server</code> and
          drop a <code className="text-aries-teal">.gguf</code> file under{' '}
          <code className="text-aries-teal">~/.aries/&lt;name&gt;/models/</code>.
        </div>
      </div>
    )
  }

  return (
    <div className="rounded-(--radius-card) border border-aries-border bg-aries-surface overflow-hidden">
      <div className="px-5 py-4 border-b border-aries-border flex items-baseline justify-between">
        <div>
          <div className="text-xs uppercase tracking-[0.18em] text-aries-text-dim">
            Available Configurations
          </div>
          <div className="text-lg font-semibold text-aries-text mt-0.5">
            {configs.length} {configs.length === 1 ? 'option' : 'options'} ranked by scheduler
          </div>
        </div>
        <Cpu size={18} strokeWidth={1.5} className="text-aries-text-dim" />
      </div>

      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="bg-aries-surface-hi/40 text-aries-text-dim text-xs uppercase tracking-wider">
            <tr>
              <th className="px-5 py-3 text-left font-medium">Model</th>
              <th className="px-5 py-3 text-left font-medium">Type</th>
              <th className="px-5 py-3 text-left font-medium">Devices</th>
              <th className="px-5 py-3 text-right font-medium">Est. tok/s</th>
              <th className="px-5 py-3 text-right font-medium">Score</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-aries-border/60">
            {configs.map((c, i) => (
              <tr key={c.config_id} className="hover:bg-aries-surface-hi/30 transition-colors">
                <td className="px-5 py-3.5">
                  <div className="text-aries-text font-medium">{c.model_name}</div>
                  <div className="text-[11px] text-aries-text-dim font-mono">{c.config_id}</div>
                </td>
                <td className="px-5 py-3.5">{configToneBadge(c.config_type)}</td>
                <td className="px-5 py-3.5">
                  <div className="flex items-center gap-1.5">
                    <span className="text-aries-text">{c.devices?.length || 0}</span>
                    <span className="text-aries-text-dim text-xs">
                      {c.devices?.length === 1 ? 'device' : 'devices'}
                    </span>
                  </div>
                </td>
                <td className="px-5 py-3.5 text-right">
                  <span className="text-aries-text font-mono">{c.estimated_tok_s}</span>
                </td>
                <td className="px-5 py-3.5 text-right">
                  <span className={'font-mono ' + (i === 0 ? 'text-aries-teal font-semibold' : 'text-aries-text')}>
                    {(c.weighted_score * 100).toFixed(0)}
                  </span>
                  {i === 0 && <span className="ml-1 text-[10px] text-aries-teal">★</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function ActiveSession({ active }) {
  const isActive = !!active
  return (
    <div className="relative rounded-(--radius-card) border border-aries-border bg-aries-surface p-5 overflow-hidden">
      {isActive && (
        <div className="absolute inset-x-0 top-0 h-px aries-shimmer pointer-events-none" />
      )}
      <div className="flex items-baseline justify-between mb-3">
        <div>
          <div className="text-xs uppercase tracking-[0.18em] text-aries-text-dim">
            Active Inference Session
          </div>
          <div className="text-lg font-semibold text-aries-text mt-0.5">
            {isActive ? active.model_name : 'Idle'}
          </div>
        </div>
        <Zap size={18} strokeWidth={1.5}
             className={isActive ? 'text-aries-teal aries-pulse' : 'text-aries-text-dim'} />
      </div>

      {isActive ? (
        <div className="space-y-3 text-sm">
          <div className="flex items-baseline justify-between">
            <span className="text-aries-text-dim">Type</span>
            {configToneBadge(active.config_type)}
          </div>
          <div className="flex items-baseline justify-between">
            <span className="text-aries-text-dim">Workers ready</span>
            <span className="font-mono text-aries-text">
              {Object.values(active.workers_ready || {}).filter(Boolean).length}/{Object.keys(active.workers_ready || {}).length || 0}
            </span>
          </div>
          <div className="flex items-baseline justify-between">
            <span className="text-aries-text-dim">Session</span>
            <span className="font-mono text-[11px] text-aries-text truncate ml-3 max-w-[200px]">
              {active.config_id}
            </span>
          </div>
        </div>
      ) : (
        <div className="text-sm text-aries-text-dim py-4">
          Nothing running right now. The next <code className="text-aries-teal">aries invoke</code> will
          show up here when it routes to a distributed configuration.
        </div>
      )}
    </div>
  )
}

function ConfigDetails({ configs }) {
  if (!configs || configs.length === 0) return null
  // Show the scoring breakdown for the top 3 picks.
  const top = configs.slice(0, 3)
  return (
    <div className="rounded-(--radius-card) border border-aries-border bg-aries-surface p-5">
      <div className="text-xs uppercase tracking-[0.18em] text-aries-text-dim mb-1">
        Scoring breakdown
      </div>
      <div className="text-lg font-semibold text-aries-text mb-4">
        How the scheduler picked
      </div>
      <div className="space-y-5">
        {top.map((c) => (
          <div key={c.config_id}>
            <div className="flex items-baseline justify-between mb-2">
              <div className="text-sm font-medium text-aries-text">{c.model_name}</div>
              {configToneBadge(c.config_type)}
            </div>
            <div className="space-y-1.5">
              <ScoreBar value={c.privacy_score} label="privacy" />
              <ScoreBar value={c.capability_score} label="capability" />
              <ScoreBar value={c.cost_score} label="cost" />
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

function RecentTasks({ tasks }) {
  return (
    <div className="rounded-(--radius-card) border border-aries-border bg-aries-surface p-5">
      <div className="flex items-baseline justify-between mb-3">
        <div>
          <div className="text-xs uppercase tracking-[0.18em] text-aries-text-dim">
            Recent Tasks
          </div>
          <div className="text-lg font-semibold text-aries-text mt-0.5">
            Last {Math.min(tasks?.length || 0, 20)} receipts
          </div>
        </div>
      </div>

      {!tasks || tasks.length === 0 ? (
        <div className="text-sm text-aries-text-dim py-6 text-center">
          No completed tasks yet.
        </div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="text-aries-text-dim text-xs uppercase tracking-wider border-b border-aries-border">
              <tr>
                <th className="py-2 pr-3 text-left font-medium">Task</th>
                <th className="py-2 px-3 text-left font-medium">Action</th>
                <th className="py-2 px-3 text-left font-medium">Model</th>
                <th className="py-2 px-3 text-right font-medium">Tokens</th>
                <th className="py-2 px-3 text-right font-medium">Latency</th>
                <th className="py-2 pl-3 text-right font-medium">Status</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-aries-border/40">
              {tasks.map((t) => (
                <tr key={t.task_id} className="hover:bg-aries-surface-hi/30 transition-colors">
                  <td className="py-2.5 pr-3 font-mono text-[11px] text-aries-text truncate max-w-[140px]">
                    {t.task_id}
                  </td>
                  <td className="py-2.5 px-3 text-aries-text">{t.action}</td>
                  <td className="py-2.5 px-3 text-aries-text-dim truncate max-w-[160px]">
                    {t.model_used || '—'}
                  </td>
                  <td className="py-2.5 px-3 text-right font-mono text-aries-text">
                    {t.tokens_used || 0}
                  </td>
                  <td className="py-2.5 px-3 text-right font-mono text-aries-text">
                    {t.latency_ms ? t.latency_ms.toFixed(0) + 'ms' : '—'}
                  </td>
                  <td className="py-2.5 pl-3 text-right">
                    {t.status === 'error' ? (
                      <X size={14} className="text-aries-error inline" />
                    ) : (
                      <Check size={14} className="text-aries-success inline" />
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

/* ------------------------------------------------------------ page */

export default function Inference() {
  const { data: inferenceData } = useAPI('inference', 3000)
  const { data: tasksData } = useAPI('tasks', 4000)
  const configs = inferenceData?.configurations || []
  const active = inferenceData?.active_inference || null
  const tasks = tasksData?.tasks || []

  return (
    <div className="space-y-6">
      <ConfigsTable configs={configs} />

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <ActiveSession active={active} />
        <ConfigDetails configs={configs} />
      </div>

      <RecentTasks tasks={tasks} />
    </div>
  )
}
