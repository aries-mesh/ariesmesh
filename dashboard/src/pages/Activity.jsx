import { useMemo, useState } from 'react'
import { Filter, Pause, Play, Trash2 } from 'lucide-react'
import useSSE from '../hooks/useSSE'
import EventCard from '../components/EventCard'

const FILTER_GROUPS = [
  { key: 'all', label: 'All' },
  { key: 'peer', label: 'Peers', match: (t) => t.startsWith('peer_') },
  { key: 'invoke', label: 'Invocations', match: (t) => t.includes('invoke') },
  { key: 'handoff', label: 'Handoffs', match: (t) => t.startsWith('handoff_') },
  { key: 'agent', label: 'Agents', match: (t) => t.startsWith('agent_') },
  { key: 'error', label: 'Errors', match: (t) => t.toLowerCase().includes('error') },
]

export default function Activity() {
  const { events, connected } = useSSE(200)
  const [filter, setFilter] = useState('all')
  const [paused, setPaused] = useState(false)
  const [snapshot, setSnapshot] = useState([])

  const visible = useMemo(() => {
    const source = paused ? snapshot : events
    if (filter === 'all') return source
    const group = FILTER_GROUPS.find((g) => g.key === filter)
    if (!group || !group.match) return source
    return source.filter((e) => group.match(e.type || ''))
  }, [events, snapshot, filter, paused])

  const togglePause = () => {
    if (!paused) setSnapshot(events)
    setPaused((p) => !p)
  }

  const groupCounts = useMemo(() => {
    const counts = {}
    FILTER_GROUPS.forEach((g) => {
      if (g.key === 'all') counts.all = events.length
      else counts[g.key] = events.filter((e) => g.match(e.type || '')).length
    })
    return counts
  }, [events])

  return (
    <div className="space-y-5">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <div className="text-xs uppercase tracking-[0.18em] text-aries-text-dim">Activity Feed</div>
          <div className="text-2xl font-semibold text-aries-text mt-0.5">
            {connected ? 'Live event stream' : 'Reconnecting…'}
          </div>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={togglePause}
            className={
              'flex items-center gap-2 px-3 py-1.5 rounded-md text-sm font-medium ' +
              (paused
                ? 'bg-aries-warning/15 text-aries-warning ring-1 ring-aries-warning/30'
                : 'bg-aries-surface text-aries-text ring-1 ring-aries-border hover:bg-aries-surface-hi')
            }
          >
            {paused ? <Play size={14} strokeWidth={1.8} /> : <Pause size={14} strokeWidth={1.8} />}
            {paused ? 'Resume' : 'Pause'}
          </button>
        </div>
      </div>

      {/* Filter chips */}
      <div className="rounded-(--radius-card) border border-aries-border bg-aries-surface p-4">
        <div className="flex items-center gap-2 flex-wrap">
          <Filter size={14} strokeWidth={1.8} className="text-aries-text-dim" />
          {FILTER_GROUPS.map((g) => (
            <button
              key={g.key}
              onClick={() => setFilter(g.key)}
              className={
                'flex items-center gap-1.5 px-2.5 py-1 rounded-md text-xs font-medium transition-colors ' +
                (filter === g.key
                  ? 'bg-aries-teal/15 text-aries-teal ring-1 ring-aries-teal/30'
                  : 'text-aries-text-dim hover:text-aries-text hover:bg-aries-surface-hi')
              }
            >
              {g.label}
              <span className="font-mono text-[10px] opacity-75">
                {groupCounts[g.key] ?? 0}
              </span>
            </button>
          ))}
        </div>
      </div>

      {/* Event list */}
      <div className="rounded-(--radius-card) border border-aries-border bg-aries-surface p-3">
        {visible.length === 0 ? (
          <div className="text-center py-16">
            <Trash2 size={26} strokeWidth={1.5} className="mx-auto text-aries-text-dim/60 mb-3" />
            <div className="text-aries-text text-sm">
              No events match this filter.
            </div>
            <div className="text-xs text-aries-text-dim mt-1">
              {paused
                ? 'Resume the feed or change filters to see live events.'
                : 'Run a command on the node to start generating events.'}
            </div>
          </div>
        ) : (
          <div className="space-y-1.5">
            {visible.map((ev, i) => (
              <EventCard
                key={`${ev.timestamp}-${i}-${ev.type}`}
                event={ev}
                animated={i === 0 && !paused}
              />
            ))}
          </div>
        )}
      </div>

      <div className="text-center text-[11px] text-aries-text-dim">
        Showing {visible.length} of {events.length} buffered events
        {paused && <span className="text-aries-warning"> · paused</span>}
      </div>
    </div>
  )
}
