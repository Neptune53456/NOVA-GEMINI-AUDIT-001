import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { getCockpit, type CockpitResponse, type ConnectionStatus, type NovaStateResponse } from '../api/novaApi'
import { NovaStateContext } from './NovaStateContext'

const offlineState: NovaStateResponse = { state: 'idle', label: 'Nova hors connexion', busy: false, message: 'L’API locale Nova est indisponible. Vous pouvez continuer à parcourir l’interface.' }

export function NovaStateProvider({ children }: { children: ReactNode }) {
  const [snapshot, setSnapshot] = useState<CockpitResponse>()
  const [connectionStatus, setConnectionStatus] = useState<ConnectionStatus>('connecting')
  const mountedRef = useRef(false)
  const inFlightRef = useRef(false)
  const requestRef = useRef<AbortController | null>(null)
  const refresh = useCallback(async () => {
    if (inFlightRef.current) return
    inFlightRef.current = true
    const controller = new AbortController()
    requestRef.current = controller
    try {
      const nextSnapshot = await getCockpit(controller.signal)
      if (mountedRef.current) { setSnapshot(nextSnapshot); setConnectionStatus('connected') }
    } catch {
      if (mountedRef.current) setConnectionStatus('disconnected')
    } finally {
      if (requestRef.current === controller) requestRef.current = null
      inFlightRef.current = false
    }
  }, [])
  useEffect(() => {
    mountedRef.current = true
    const initialRefresh = window.setTimeout(() => void refresh(), 0)
    const timer = window.setInterval(() => void refresh(), 3_000)
    return () => { mountedRef.current = false; window.clearTimeout(initialRefresh); window.clearInterval(timer); requestRef.current?.abort() }
  }, [refresh])
  const state = snapshot?.nova ?? offlineState
  return <NovaStateContext.Provider value={{ ...state, connectionStatus, snapshot, lastUpdated: snapshot ? new Date(snapshot.generated_at) : undefined, refresh }}>{children}</NovaStateContext.Provider>
}
