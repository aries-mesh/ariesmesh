import { forwardRef } from 'react'
import { Laptop, Monitor, Smartphone, Tablet, Cpu } from 'lucide-react'

/**
 * Map (platform, name) → lucide icon component.
 * Name heuristics win over platform because users name their devices
 * descriptively ("rohit-pixel-8", "kitchen-ipad", "office-tower").
 */
export function iconForDevice(platform, name) {
  const lower = (name || '').toLowerCase()
  if (/tablet|ipad/.test(lower)) return Tablet
  if (/phone|mobile|pixel|iphone|android/.test(lower)) return Smartphone
  if (/desktop|workstation|tower|pc/.test(lower)) return Monitor
  if (/laptop|book|mac/.test(lower)) return Laptop

  const p = (platform || '').toLowerCase()
  if (p === 'ios' || p === 'android') return Smartphone
  if (p === 'ipados') return Tablet
  if (p === 'macos' || p === 'linux' || p === 'windows') return Laptop
  return Cpu
}

export function labelForPlatform(platform) {
  const p = (platform || '').toLowerCase()
  if (p === 'macos') return 'macOS'
  if (p === 'linux') return 'Linux'
  if (p === 'windows') return 'Windows'
  if (p === 'ios') return 'iOS'
  if (p === 'android') return 'Android'
  if (p === 'ipados') return 'iPadOS'
  return platform || 'unknown'
}

/**
 * Circular device node rendered as an HTML element (so it can host a real
 * lucide-react icon and a rich tooltip). Positioned absolutely by the parent
 * using `style={{ left, top }}` percentages — the parent container is
 * responsible for layout math.
 */
const DeviceCircle = forwardRef(function DeviceCircle(
  {
    name,
    platform,
    isSelf = false,
    online = true,
    onHover,
    onLeave,
    style,
  },
  ref,
) {
  const Icon = iconForDevice(platform, name)
  const size = isSelf ? 96 : 80
  const iconSize = isSelf ? 36 : 30

  // Build the visual tone based on state.
  const ringColor = isSelf
    ? 'border-aries-teal/70'
    : online
      ? 'border-aries-border'
      : 'border-aries-error/40'
  const bg = isSelf
    ? 'bg-gradient-to-br from-aries-surface-hi via-aries-surface to-aries-indigo'
    : 'bg-aries-surface'
  const iconColor = isSelf
    ? 'text-aries-teal'
    : online
      ? 'text-aries-text'
      : 'text-aries-text-dim'
  const opacity = online ? '' : 'opacity-55'

  return (
    <div
      ref={ref}
      style={style}
      className={`absolute -translate-x-1/2 -translate-y-1/2 flex flex-col items-center group cursor-default ${opacity}`}
      onMouseEnter={onHover}
      onMouseLeave={onLeave}
    >
      {/* Outer slowly-rotating accent ring for the host device */}
      {isSelf && (
        <div
          className="aries-ring absolute pointer-events-none"
          style={{
            width: size + 24,
            height: size + 24,
            top: '50%',
            left: '50%',
            transform: 'translate(-50%, calc(-50% - 14px))',
          }}
        >
          <svg width={size + 24} height={size + 24} viewBox="0 0 100 100">
            <circle
              cx="50" cy="50" r="48"
              fill="none"
              stroke="rgba(0, 212, 170, 0.35)"
              strokeWidth="0.6"
              strokeDasharray="2 6"
            />
          </svg>
        </div>
      )}

      {/* Circle node */}
      <div
        className={`relative rounded-full border ${ringColor} ${bg}
          flex items-center justify-center
          transition-transform duration-200 ease-out
          group-hover:scale-[1.06]
          ${isSelf ? 'aries-halo' : 'shadow-[0_4px_18px_rgba(0,0,0,0.35)]'}
        `}
        style={{ width: size, height: size }}
      >
        <Icon size={iconSize} strokeWidth={1.6} className={iconColor} />

        {/* Status dot */}
        <span
          className={
            `absolute top-1 right-1 inline-block rounded-full ring-2 ring-aries-dark ` +
            (online ? 'bg-aries-success aries-pulse' : 'bg-aries-error')
          }
          style={{ width: 10, height: 10 }}
          aria-label={online ? 'online' : 'offline'}
        />
      </div>

      {/* Label */}
      <div className="mt-3 text-center max-w-[140px]">
        <div className={
          'text-sm truncate font-medium ' +
          (isSelf ? 'text-aries-teal' : 'text-aries-text')
        }>
          {name || '—'}
        </div>
        <div className="text-[10px] uppercase tracking-[0.18em] text-aries-text-dim">
          {isSelf ? 'This Device' : labelForPlatform(platform)}
        </div>
      </div>
    </div>
  )
})

export default DeviceCircle
