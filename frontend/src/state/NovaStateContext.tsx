import { createContext, useContext } from 'react'
import type { CockpitResponse, ConnectionStatus, NovaStateResponse } from '../api/novaApi'

export interface NovaStateContextValue extends NovaStateResponse { connectionStatus: ConnectionStatus; snapshot?: CockpitResponse; lastUpdated?: Date; refresh: () => Promise<void> }
export const NovaStateContext = createContext<NovaStateContextValue | null>(null)

export function useNovaState(): NovaStateContextValue {
  const context = useContext(NovaStateContext)
  if (!context) throw new Error('useNovaState must be used inside NovaStateProvider')
  return context
}
