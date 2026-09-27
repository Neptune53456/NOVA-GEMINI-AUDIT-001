import { useNovaState } from '../state/NovaStateContext'

export function NovaPresence() {
  const { state, label, message, connectionStatus } = useNovaState()
  const connectionLabel = connectionStatus === 'connected' ? 'API connectée' : connectionStatus === 'connecting' ? 'Connexion…' : 'Hors connexion'
  return <aside className="presence-panel" data-state={state} data-connection={connectionStatus}><div className="presence-header"><span>PRÉSENCE NOVA</span><span className="presence-live">{connectionLabel}</span></div><div className="presence-stage" aria-label={`Nova : ${label}`}><div className="orb-halo"><div className="orb"><span /></div></div><div className="presence-copy"><strong>{label}</strong><span>{message}</span></div></div><div className="presence-stats"><div><span>Modèle</span><strong>Nova Core</strong></div><div><span>Contexte</span><strong>Local</strong></div></div><p className="presence-note">Un espace réservé accueillera ici la future présence 3D de Nova.</p></aside>
}
