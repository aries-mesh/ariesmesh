import { useEffect, useState } from 'react'

/**
 * Poll a JSON endpoint every `intervalMs` ms.
 * Returns { data, error, loading }.
 *
 * The endpoint is appended to `/api/`. `endpoint=null` disables polling
 * (useful for conditional fetches).
 */
export default function useAPI(endpoint, intervalMs = 3000) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    if (!endpoint) return

    let cancelled = false
    const fetchData = async () => {
      try {
        const res = await fetch(`/api/${endpoint}`)
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        const body = await res.json()
        if (!cancelled) {
          setData(body)
          setError(null)
        }
      } catch (e) {
        if (!cancelled) setError(e.message || String(e))
      } finally {
        if (!cancelled) setLoading(false)
      }
    }

    fetchData()
    const timer = setInterval(fetchData, intervalMs)
    return () => {
      cancelled = true
      clearInterval(timer)
    }
  }, [endpoint, intervalMs])

  return { data, error, loading }
}
