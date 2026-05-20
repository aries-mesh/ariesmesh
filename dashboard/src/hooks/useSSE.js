import { useEffect, useRef, useState } from 'react'

/**
 * Subscribe to /api/events (Server-Sent Events) and accumulate the last
 * `maxEvents` events, newest first.
 *
 * Returns { events, connected }. The hook reconnects automatically on
 * transient errors after a short backoff.
 */
export default function useSSE(maxEvents = 50) {
  const [events, setEvents] = useState([])
  const [connected, setConnected] = useState(false)
  const sourceRef = useRef(null)

  useEffect(() => {
    let cancelled = false
    let backoff = 1000

    const open = () => {
      if (cancelled) return
      const src = new EventSource('/api/events')
      sourceRef.current = src

      src.onopen = () => {
        if (cancelled) return
        setConnected(true)
        backoff = 1000
      }

      src.onmessage = (e) => {
        if (cancelled) return
        try {
          const ev = JSON.parse(e.data)
          setEvents((prev) => [ev, ...prev].slice(0, maxEvents))
        } catch {
          // ignore malformed event
        }
      }

      src.onerror = () => {
        if (cancelled) return
        setConnected(false)
        src.close()
        // Exponential backoff up to 10s
        setTimeout(() => {
          backoff = Math.min(backoff * 2, 10000)
          open()
        }, backoff)
      }
    }

    open()
    return () => {
      cancelled = true
      sourceRef.current?.close()
    }
  }, [maxEvents])

  return { events, connected }
}
