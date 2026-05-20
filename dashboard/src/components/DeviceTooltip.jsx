import { labelForPlatform } from './DeviceCircle'

function formatLastSeen(secs) {
  if (secs == null) return '—'
  if (secs < 5) return 'just now'
  if (secs < 60) return `${Math.floor(secs)}s ago`
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`
  return `${Math.floor(secs / 86400)}d ago`
}

/**
 * Hover-card for a device node. Positioned absolutely by the parent using
 * percentage left/top — we slide it down so it sits beneath the circle.
 */
export default function DeviceTooltip({ device, style }) {
  if (!device) return null
  const {
    name,
    platform,
    isSelf,
    online,
    did_short,
    host,
    port,
    latency_ms,
    last_seen_seconds_ago,
    uptime_seconds,
  } = device

  return (
    <div
      className="aries-tooltip absolute z-20 pointer-events-none"
      style={style}
    >
      <div className="w-[260px] rounded-xl border border-aries-border bg-aries-surface/95 backdrop-blur-md shadow-[0_8px_30px_rgba(0,0,0,0.55)] p-3.5">
        {/* Header */}
        <div className="flex items-baseline justify-between gap-2 mb-2">
          <div className="min-w-0">
            <div className="text-sm font-semibold text-aries-text truncate">{name || '—'}</div>
            <div className="text-[10px] uppercase tracking-[0.18em] text-aries-text-dim">
              {isSelf ? 'host device' : 'peer'}
            </div>
          </div>
          <span
            className={
              'inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-[10px] font-medium ' +
              (online
                ? 'bg-aries-success/15 text-aries-success ring-1 ring-aries-success/30'
                : 'bg-aries-error/15 text-aries-error ring-1 ring-aries-error/30')
            }
          >
            <span className={'w-1.5 h-1.5 rounded-full bg-current ' + (online ? 'aries-pulse' : '')} />
            {online ? 'online' : 'offline'}
          </span>
        </div>

        {/* Body */}
        <dl className="grid grid-cols-[88px_1fr] gap-y-1.5 gap-x-2 text-[11px] leading-tight">
          <dt className="text-aries-text-dim">Platform</dt>
          <dd className="text-aries-text">{labelForPlatform(platform)}</dd>

          <dt className="text-aries-text-dim">Device DID</dt>
          <dd className="text-aries-text font-mono truncate">{did_short || '—'}</dd>

          {host && (
            <>
              <dt className="text-aries-text-dim">Address</dt>
              <dd className="text-aries-text font-mono">{host}{port ? `:${port}` : ''}</dd>
            </>
          )}

          {!isSelf && (
            <>
              <dt className="text-aries-text-dim">Latency</dt>
              <dd className="text-aries-text font-mono">
                {latency_ms != null ? `${Math.round(latency_ms)} ms` : '—'}
              </dd>

              <dt className="text-aries-text-dim">Last seen</dt>
              <dd className="text-aries-text font-mono">{formatLastSeen(last_seen_seconds_ago)}</dd>
            </>
          )}

          {isSelf && uptime_seconds != null && (
            <>
              <dt className="text-aries-text-dim">Uptime</dt>
              <dd className="text-aries-text font-mono">
                {formatDuration(uptime_seconds)}
              </dd>
            </>
          )}
        </dl>
      </div>
    </div>
  )
}

function formatDuration(secs) {
  if (!secs || secs < 0) return '—'
  const h = Math.floor(secs / 3600)
  const m = Math.floor((secs % 3600) / 60)
  const s = Math.floor(secs % 60)
  if (h > 0) return `${h}h ${m}m`
  if (m > 0) return `${m}m ${s}s`
  return `${s}s`
}
