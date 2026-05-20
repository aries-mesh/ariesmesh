/**
 * Single event row used in the activity feed.
 *
 * The dot color is derived from the event type, so the feed scans visually:
 *   green   peer_connect, memory_sync
 *   blue    invoke
 *   amber   handoff_sent, handoff_recv
 *   red     anything with "error" in the type
 *   teal    everything else
 */
const TYPE_TONE = {
  peer_connect: 'bg-aries-success',
  memory_sync:  'bg-aries-success',
  invoke:       'bg-sky-400',
  invoke_distributed: 'bg-sky-400',
  agent_register: 'bg-aries-teal',
  handoff_sent: 'bg-aries-warning',
  handoff_recv: 'bg-aries-warning',
  node_start:   'bg-aries-teal',
}

function toneFor(type) {
  if (!type) return 'bg-aries-text-dim'
  if (type.toLowerCase().includes('error')) return 'bg-aries-error'
  return TYPE_TONE[type] || 'bg-aries-teal'
}

function timeAgo(ts) {
  const diff = Math.max(0, Date.now() / 1000 - ts)
  if (diff < 5) return 'just now'
  if (diff < 60) return `${Math.floor(diff)}s ago`
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`
  return `${Math.floor(diff / 86400)}d ago`
}

export default function EventCard({ event, animated = false }) {
  if (!event) return null
  const tone = toneFor(event.type)
  return (
    <div
      className={
        'flex items-start gap-3 px-3 py-2.5 rounded-lg border border-aries-border/60 ' +
        'bg-aries-surface/55 ' +
        (animated ? 'aries-slide-in' : '')
      }
    >
      <span
        className={`mt-1.5 inline-block w-1.5 h-1.5 rounded-full shrink-0 ${tone}`}
        aria-hidden
      />
      <div className="flex-1 min-w-0">
        <div className="flex items-baseline justify-between gap-3">
          <span className="text-[10px] uppercase tracking-[0.16em] font-mono text-aries-text-dim">
            {event.type}
          </span>
          <span className="text-[10px] text-aries-text-dim font-mono">
            {timeAgo(event.timestamp)}
          </span>
        </div>
        <div className="text-sm text-aries-text truncate">
          {event.description}
        </div>
      </div>
    </div>
  )
}
