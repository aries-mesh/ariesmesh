/**
 * SVG node used in the topology graph.
 *
 * Props:
 *  - x, y         absolute SVG coordinates
 *  - name         device display name
 *  - subtitle     hardware / role label (e.g. "M3 Pro · 16 GB")
 *  - isSelf       true if this is the current device → teal glow
 *  - online       true → green dot, false → red + dimmed
 */
export default function DeviceNode({ x, y, name, subtitle, isSelf, online }) {
  const w = 168
  const h = 64
  const fill = isSelf ? '#162235' : '#162235'
  const strokeColor = isSelf
    ? '#00D4AA'
    : online
      ? '#1E3450'
      : '#3E2A2E'
  const opacity = online ? 1 : 0.55
  const statusColor = online ? '#00D4AA' : '#EF4D5E'

  return (
    <g transform={`translate(${x - w / 2}, ${y - h / 2})`} opacity={opacity}>
      {isSelf && (
        <rect
          x={-3}
          y={-3}
          width={w + 6}
          height={h + 6}
          rx={14}
          fill="none"
          stroke="#00D4AA"
          strokeWidth={1}
          opacity={0.28}
        />
      )}
      <rect
        x={0}
        y={0}
        width={w}
        height={h}
        rx={11}
        fill={fill}
        stroke={strokeColor}
        strokeWidth={isSelf ? 1.5 : 1}
      />
      {/* status dot */}
      <circle cx={14} cy={20} r={4} fill={statusColor}>
        {online && (
          <animate
            attributeName="opacity"
            values="1;0.5;1"
            dur="2.2s"
            repeatCount="indefinite"
          />
        )}
      </circle>
      <text
        x={26}
        y={24}
        fill="#C8D6E5"
        fontSize="13"
        fontWeight="600"
        fontFamily="ui-sans-serif, system-ui"
      >
        {name}
      </text>
      <text
        x={14}
        y={45}
        fill="#6B7F99"
        fontSize="11"
        fontFamily="ui-sans-serif, system-ui"
      >
        {subtitle || ''}
      </text>
      {isSelf && (
        <text
          x={w - 10}
          y={18}
          textAnchor="end"
          fill="#00D4AA"
          fontSize="9"
          fontWeight="600"
          letterSpacing="0.1em"
          fontFamily="ui-sans-serif, system-ui"
        >
          THIS DEVICE
        </text>
      )}
    </g>
  )
}
