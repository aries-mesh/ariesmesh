/**
 * A horizontal progress-style bar.
 *
 * - `value` is 0..100 (or 0..1 if `unit==="fraction"`)
 * - `invert=true` flips color thresholds (used for battery: low = red)
 */
export default function HealthBar({ label, value, max = 100, unit = '%', invert = false, sublabel }) {
  const pct = Math.max(0, Math.min(100, (value / max) * 100))

  // Color thresholds. Higher is "worse" by default (e.g. CPU usage).
  // For inverted scales (e.g. battery%), lower is worse.
  let toneClass
  if (invert) {
    if (pct < 20) toneClass = 'bg-aries-error'
    else if (pct < 50) toneClass = 'bg-aries-warning'
    else toneClass = 'bg-aries-success'
  } else {
    if (pct > 80) toneClass = 'bg-aries-error'
    else if (pct > 60) toneClass = 'bg-aries-warning'
    else toneClass = 'bg-aries-success'
  }

  return (
    <div>
      <div className="flex items-baseline justify-between mb-1.5">
        <span className="text-xs text-aries-text-dim uppercase tracking-wider">{label}</span>
        <span className="text-xs text-aries-text font-mono">
          {sublabel ?? `${value.toFixed?.(0) ?? value}${unit}`}
        </span>
      </div>
      <div className="h-1.5 w-full rounded-full bg-aries-border/60 overflow-hidden">
        <div
          className={`h-full rounded-full transition-all duration-700 ease-out ${toneClass}`}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  )
}
