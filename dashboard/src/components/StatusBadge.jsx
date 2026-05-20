const TONES = {
  success: 'bg-aries-success/15 text-aries-success ring-aries-success/30',
  warning: 'bg-aries-warning/15 text-aries-warning ring-aries-warning/30',
  error:   'bg-aries-error/15 text-aries-error ring-aries-error/30',
  info:    'bg-aries-teal/10 text-aries-teal ring-aries-teal/25',
  muted:   'bg-aries-surface text-aries-text-dim ring-aries-border',
}

export default function StatusBadge({ tone = 'muted', label, pulse = false }) {
  const cls = TONES[tone] || TONES.muted
  return (
    <span className={`inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-[11px] font-medium ring-1 ring-inset ${cls}`}>
      <span
        className={
          'inline-block w-1.5 h-1.5 rounded-full bg-current ' +
          (pulse ? 'aries-pulse' : '')
        }
      />
      {label}
    </span>
  )
}
