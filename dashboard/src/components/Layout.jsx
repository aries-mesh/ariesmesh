import { NavLink, useLocation } from 'react-router-dom'
import { Activity, Cpu, Network } from 'lucide-react'
import useSSE from '../hooks/useSSE'
import useAPI from '../hooks/useAPI'
import logoUrl from '../assets/simple_icon.png'

function NavTab({ to, icon: Icon, label }) {
  return (
    <NavLink
      to={to}
      className={({ isActive }) =>
        `flex items-center gap-2 px-3 py-1.5 rounded-md text-sm font-medium transition-colors ` +
        (isActive
          ? 'bg-aries-surface-hi text-aries-teal'
          : 'text-aries-text-dim hover:text-aries-text hover:bg-aries-surface')
      }
    >
      <Icon size={15} strokeWidth={1.8} />
      {label}
    </NavLink>
  )
}

function ConnectionIndicator() {
  const { connected } = useSSE(1)
  const { data: status } = useAPI('status', 10000)
  return (
    <div className="flex items-center gap-2 text-xs">
      <span
        className={
          'aries-pulse inline-block w-1.5 h-1.5 rounded-full ' +
          (connected ? 'bg-aries-success shadow-[0_0_8px_var(--color-aries-success)]' : 'bg-aries-error')
        }
        aria-label={connected ? 'connected' : 'disconnected'}
      />
      <span className="text-aries-text-dim">
        {connected ? 'live' : 'reconnecting…'}
      </span>
      {status?.device_name && (
        <span className="text-aries-text-dim ml-3">· {status.device_name}</span>
      )}
    </div>
  )
}

function Logo() {
  return (
    <div className="flex items-center gap-3">
      <div className="relative shrink-0">
        <img
          src={logoUrl}
          alt="Aries Mesh"
          width="36"
          height="36"
          className="rounded-lg block"
        />
        {/* Soft teal halo so the icon feels alive against the dark bar */}
        <div className="absolute inset-0 rounded-lg pointer-events-none shadow-[0_0_18px_rgba(0,212,170,0.20)]" />
      </div>
      <div className="leading-tight">
        <div className="text-aries-text font-semibold tracking-wide text-sm">ARIES MESH</div>
        <div className="text-[10px] uppercase tracking-[0.18em] text-aries-text-dim -mt-0.5">
          Personal Compute Fabric
        </div>
      </div>
    </div>
  )
}

export default function Layout({ children }) {
  const location = useLocation()
  return (
    <div className="min-h-screen flex flex-col">
      <header className="sticky top-0 z-10 backdrop-blur-md bg-aries-dark/85 border-b border-aries-border">
        <div className="max-w-7xl mx-auto px-6 h-14 flex items-center justify-between">
          <Logo />
          <nav className="flex items-center gap-1">
            <NavTab to="/topology" icon={Network} label="Topology" />
            <NavTab to="/inference" icon={Cpu} label="Inference" />
            <NavTab to="/activity" icon={Activity} label="Activity" />
          </nav>
          <ConnectionIndicator />
        </div>
      </header>

      <main key={location.pathname} className="max-w-7xl mx-auto w-full px-6 py-6 flex-1">
        {children}
      </main>

      <footer className="text-center text-[11px] text-aries-text-dim py-4 border-t border-aries-border">
        Aries Mesh v0.2 · localhost-only · all traffic Noise XX encrypted
      </footer>
    </div>
  )
}
