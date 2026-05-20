import { useState } from 'react'
import { Battery, BatteryCharging, Bot, MemoryStick, Sparkles, Thermometer, Wifi } from 'lucide-react'
import useAPI from '../hooks/useAPI'
import useSSE from '../hooks/useSSE'
import DeviceCircle from '../components/DeviceCircle'
import DeviceTooltip from '../components/DeviceTooltip'
import HealthBar from '../components/HealthBar'
import StatusBadge from '../components/StatusBadge'
import EventCard from '../components/EventCard'

/* ------------------------------------------------------------------ helpers */

function formatUptime(secs) {
  if (!secs || secs < 0) return '—'
  const h = Math.floor(secs / 3600)
  const m = Math.floor((secs % 3600) / 60)
  const s = Math.floor(secs % 60)
  return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
}

/* ----------------------------------------------------------- topology graph */

/**
 * Lay peers out around the host. Returns positions in PERCENT coords so the
 * SVG (with viewBox 0..100 0..100) and the HTML overlay (with left/top in %)
 * stay in lock-step at any container width.
 */
function layoutPeers(peers) {
  const host = { x: 26, y: 50 }
  const n = peers.length
  if (n === 0) return { host, peerPositions: [] }

  // Single peer → straight across. Multiple peers → fan out to the right.
  const peerPositions = peers.map((peer, i) => {
    if (n === 1) return { peer, x: 74, y: 50 }
    if (n === 2) return { peer, x: 74, y: i === 0 ? 28 : 72 }
    if (n === 3) {
      const ys = [22, 50, 78]
      return { peer, x: 74, y: ys[i] }
    }
    // 4+ peers: arrange on an arc
    const angle = -55 + (110 / (n - 1)) * i
    const rad = (angle * Math.PI) / 180
    return {
      peer,
      x: host.x + 56 * Math.cos(rad),
      y: host.y + 46 * Math.sin(rad),
    }
  })
  return { host, peerPositions }
}

function TopologyGraph({ status, peers }) {
  const [hovered, setHovered] = useState(null)
  const { host, peerPositions } = layoutPeers(peers)
  const offlineCount = peers.filter((p) => !p.connected).length

  // Build a unified data shape for the tooltip so hover handlers don't have
  // to special-case "self" vs "peer".
  const hostDeviceForTooltip = {
    name: status?.device_name,
    platform: status?.platform,
    isSelf: true,
    online: true,
    did_short: status?.device_did_short,
    uptime_seconds: status?.uptime_seconds,
    host: '127.0.0.1',
    port: status?.port,
  }

  return (
    <div className="relative rounded-(--radius-card) border border-aries-border bg-aries-surface/40 overflow-hidden">
      {/* Dot grid backdrop */}
      <div
        className="absolute inset-0 pointer-events-none opacity-[0.05]"
        style={{
          backgroundImage:
            'radial-gradient(circle at 1px 1px, #00D4AA 1px, transparent 0)',
          backgroundSize: '34px 34px',
        }}
      />
      {/* Header */}
      <div className="relative flex items-center justify-between px-5 pt-4 pb-2 z-10">
        <div>
          <div className="text-xs uppercase tracking-[0.18em] text-aries-text-dim">
            Mesh Topology
          </div>
          <div className="text-lg font-semibold text-aries-text">
            {peers.length + 1} device{peers.length === 0 ? '' : 's'}
            {offlineCount > 0 && (
              <span className="text-aries-text-dim text-sm font-normal ml-2">
                · {offlineCount} offline
              </span>
            )}
          </div>
        </div>
        <StatusBadge
          tone="info"
          label={`Protocol ${status?.protocol_version || 'v0.2'} · encrypted`}
        />
      </div>

      {/* Stage — SVG draws connecting edges, HTML overlays draw device circles */}
      <div className="relative w-full h-[420px]">
        <svg
          className="absolute inset-0 w-full h-full"
          viewBox="0 0 100 100"
          preserveAspectRatio="none"
        >
          {peerPositions.map(({ peer, x, y }) => {
            const offline = !peer.connected
            return (
              <g key={peer.device_did}>
                <line
                  x1={host.x}
                  y1={host.y}
                  x2={x}
                  y2={y}
                  stroke={offline ? '#3E2A2E' : '#00D4AA'}
                  strokeOpacity={offline ? 0.55 : 0.5}
                  strokeWidth={0.2}
                  className={offline ? '' : 'aries-edge'}
                  style={{ vectorEffect: 'non-scaling-stroke' }}
                />
              </g>
            )
          })}
        </svg>

        {/* Latency labels — rendered as HTML so they stay readable + crisp */}
        {peerPositions.map(({ peer, x, y }) => (
          <div
            key={`label-${peer.device_did}`}
            className="absolute -translate-x-1/2 -translate-y-1/2 pointer-events-none"
            style={{ left: `${(host.x + x) / 2}%`, top: `${(host.y + y) / 2}%` }}
          >
            <div className="text-[10px] font-mono text-aries-text bg-aries-dark/95 border border-aries-border rounded-full px-2 py-0.5 shadow-sm">
              {peer.latency_ms != null ? `${Math.round(peer.latency_ms)} ms` : '—'}
            </div>
          </div>
        ))}

        {/* Empty state */}
        {peers.length === 0 && (
          <div
            className="absolute -translate-x-1/2 -translate-y-1/2 text-center"
            style={{ left: '74%', top: '50%' }}
          >
            <div className="text-aries-text-dim text-sm">No peers connected</div>
            <div className="text-aries-text-dim text-xs mt-1 opacity-75">
              Run <code className="text-aries-teal">aries pair --invite</code> to add one
            </div>
          </div>
        )}

        {/* Host */}
        <DeviceCircle
          name={status?.device_name || 'this-device'}
          platform={status?.platform}
          isSelf
          online
          onHover={() =>
            setHovered({ kind: 'self', position: host, data: hostDeviceForTooltip })
          }
          onLeave={() => setHovered((h) => (h?.kind === 'self' ? null : h))}
          style={{ left: `${host.x}%`, top: `${host.y}%` }}
        />

        {/* Peers */}
        {peerPositions.map(({ peer, x, y }) => (
          <DeviceCircle
            key={peer.device_did}
            name={peer.name || peer.device_did_short || 'peer'}
            platform={peer.platform}
            online={peer.connected}
            onHover={() =>
              setHovered({
                kind: 'peer',
                position: { x, y },
                data: { ...peer, isSelf: false, online: peer.connected },
              })
            }
            onLeave={() => setHovered((h) => (h?.kind === 'peer' && h.data.device_did === peer.device_did ? null : h))}
            style={{ left: `${x}%`, top: `${y}%` }}
          />
        ))}

        {/* Tooltip — positioned beneath the hovered circle */}
        {hovered && (
          <DeviceTooltip
            device={hovered.data}
            style={{
              left: `${hovered.position.x}%`,
              top: `${hovered.position.y}%`,
            }}
          />
        )}
      </div>
    </div>
  )
}

/* ----------------------------------------------------------- side info cards */

function ThisDeviceCard({ status, health }) {
  const ramTotal = health?.ram_total_gb ?? 0
  const ramUsed = Math.max(ramTotal - (health?.ram_available_gb ?? 0), 0)
  const ramPct = ramTotal ? (ramUsed / ramTotal) * 100 : 0

  return (
    <div className="rounded-(--radius-card) border border-aries-border bg-aries-surface p-5">
      <div className="flex items-baseline justify-between mb-1">
        <div>
          <div className="text-xs uppercase tracking-[0.18em] text-aries-text-dim">This Device</div>
          <div className="text-lg font-semibold text-aries-text mt-0.5">
            {status?.device_name || '—'}
          </div>
        </div>
        <div className="text-right">
          <div className="text-[10px] uppercase tracking-wider text-aries-text-dim">Uptime</div>
          <div className="font-mono text-sm text-aries-teal">
            {formatUptime(status?.uptime_seconds)}
          </div>
        </div>
      </div>
      <div className="text-[11px] font-mono text-aries-text-dim mb-4 truncate">
        {status?.device_did_short || ''}
      </div>

      <div className="space-y-3">
        <HealthBar
          label="CPU"
          value={health?.cpu_percent ?? 0}
          sublabel={`${(health?.cpu_percent ?? 0).toFixed(0)}%`}
        />
        <HealthBar
          label="RAM"
          value={ramPct}
          sublabel={`${ramUsed.toFixed(1)}/${ramTotal.toFixed(1)} GB`}
        />
        {health?.battery_pct != null && (
          <HealthBar
            label="Battery"
            value={health.battery_pct}
            invert
            sublabel={`${Math.round(health.battery_pct)}%${health.charging ? ' ⚡' : ''}`}
          />
        )}
      </div>

      <div className="mt-4 grid grid-cols-2 gap-x-4 gap-y-2 text-xs">
        <div className="flex items-center gap-1.5 text-aries-text-dim">
          <Thermometer size={13} strokeWidth={1.8} />
          <span>Thermal</span>
        </div>
        <div className="text-aries-text text-right capitalize">
          {health?.thermal || 'unknown'}
        </div>

        <div className="flex items-center gap-1.5 text-aries-text-dim">
          <Wifi size={13} strokeWidth={1.8} />
          <span>Network</span>
        </div>
        <div className="text-aries-text text-right">
          {(health?.network_type || 'unknown')}{' '}
          <span className="text-aries-text-dim">
            · {Math.round(health?.bandwidth_mbps || 0)} MB/s
          </span>
        </div>

        <div className="flex items-center gap-1.5 text-aries-text-dim">
          {health?.charging ? <BatteryCharging size={13} strokeWidth={1.8} /> : <Battery size={13} strokeWidth={1.8} />}
          <span>Health</span>
        </div>
        <div className="text-aries-text text-right">
          {((health?.health_score ?? 0) * 100).toFixed(0)}%
        </div>
      </div>
    </div>
  )
}

function AgentsCard({ agents }) {
  return (
    <div className="rounded-(--radius-card) border border-aries-border bg-aries-surface p-5">
      <div className="flex items-baseline justify-between mb-3">
        <div>
          <div className="text-xs uppercase tracking-[0.18em] text-aries-text-dim">Agents</div>
          <div className="text-lg font-semibold text-aries-text mt-0.5">
            {agents.length} registered
          </div>
        </div>
        <Bot size={18} strokeWidth={1.5} className="text-aries-text-dim" />
      </div>

      {agents.length === 0 ? (
        <div className="text-sm text-aries-text-dim py-6 text-center">
          No agents registered.<br />
          <code className="text-xs text-aries-teal">aries register --vendor mock --model demo</code>
        </div>
      ) : (
        <div className="space-y-2.5 max-h-[220px] overflow-y-auto pr-1">
          {agents.map((a) => (
            <div key={a.agent_did} className="flex items-center justify-between">
              <div className="min-w-0">
                <div className="text-sm text-aries-text truncate">{a.name}</div>
                <div className="text-[11px] text-aries-text-dim font-mono truncate">
                  {a.vendor}/{a.model || '—'}
                </div>
              </div>
              <div className="flex flex-col items-end gap-0.5 ml-3 shrink-0">
                <StatusBadge
                  tone={a.locality === 'local' ? 'success' : 'info'}
                  label={a.locality}
                />
                <span className="text-[10px] text-aries-text-dim font-mono">{a.cost_class}</span>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function MemoryCard({ memory }) {
  const ns = memory?.by_namespace || { context: 0, memory: 0, cache: 0 }
  const total = memory?.total_keys ?? 0
  return (
    <div className="rounded-(--radius-card) border border-aries-border bg-aries-surface p-5">
      <div className="flex items-baseline justify-between mb-3">
        <div>
          <div className="text-xs uppercase tracking-[0.18em] text-aries-text-dim">Shared Memory</div>
          <div className="text-lg font-semibold text-aries-text mt-0.5">
            {total} key{total === 1 ? '' : 's'}
          </div>
        </div>
        <MemoryStick size={18} strokeWidth={1.5} className="text-aries-text-dim" />
      </div>

      <div className="space-y-2 text-sm">
        {[
          { label: 'context://', count: ns.context, tint: 'text-aries-teal' },
          { label: 'memory://', count: ns.memory, tint: 'text-sky-300' },
          { label: 'cache://', count: ns.cache, tint: 'text-aries-warning' },
        ].map((row) => (
          <div key={row.label} className="flex items-baseline justify-between">
            <span className={`font-mono text-xs ${row.tint}`}>{row.label}</span>
            <span className="text-aries-text font-mono">{row.count}</span>
          </div>
        ))}
      </div>

      <div className="mt-4 pt-3 border-t border-aries-border/60 grid grid-cols-2 gap-2 text-xs">
        <div className="text-aries-text-dim">Lamport clock</div>
        <div className="text-right font-mono text-aries-text">{memory?.lamport_clock ?? 0}</div>
        <div className="text-aries-text-dim">Append logs</div>
        <div className="text-right font-mono text-aries-text">
          {memory?.total_logs ?? 0} · {memory?.log_entries ?? 0} entries
        </div>
      </div>
    </div>
  )
}

/* ----------------------------------------------------------- activity panel */

function ActivityFeed({ events }) {
  return (
    <div className="rounded-(--radius-card) border border-aries-border bg-aries-surface p-5">
      <div className="flex items-baseline justify-between mb-3">
        <div>
          <div className="text-xs uppercase tracking-[0.18em] text-aries-text-dim">Recent Activity</div>
          <div className="text-lg font-semibold text-aries-text mt-0.5">
            Live event feed
          </div>
        </div>
        <Sparkles size={18} strokeWidth={1.5} className="text-aries-teal" />
      </div>

      {events.length === 0 ? (
        <div className="text-sm text-aries-text-dim py-6 text-center">
          Waiting for events…<br />
          <span className="text-xs">Tasks, peer connections, and handoffs appear here in real time.</span>
        </div>
      ) : (
        <div className="space-y-2 max-h-[280px] overflow-y-auto pr-1">
          {events.slice(0, 10).map((ev, i) => (
            <EventCard key={`${ev.timestamp}-${i}`} event={ev} animated={i === 0} />
          ))}
        </div>
      )}
    </div>
  )
}

/* ----------------------------------------------------------- page composition */

export default function Topology() {
  const { data: status } = useAPI('status', 5000)
  const { data: health } = useAPI('health', 2000)
  const { data: peersData } = useAPI('peers', 3000)
  const { data: agentsData } = useAPI('agents', 5000)
  const { data: memory } = useAPI('memory', 3000)
  const { events } = useSSE(50)

  const peers = peersData?.peers || []
  const agents = agentsData?.agents || []

  return (
    <div className="space-y-6">
      <TopologyGraph status={status} peers={peers} />

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        <ThisDeviceCard status={status} health={health} />
        <AgentsCard agents={agents} />
        <MemoryCard memory={memory} />
      </div>

      <ActivityFeed events={events} />
    </div>
  )
}
