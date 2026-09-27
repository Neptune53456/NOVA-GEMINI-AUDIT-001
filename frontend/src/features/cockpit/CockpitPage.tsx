import { useEffect, useState } from 'react'
import { useNovaState } from '../../state/NovaStateContext'
import { getProjectBrain, type ComponentStatus, type ProjectBrainStatus } from '../../api/novaApi'

const labels: Record<ComponentStatus, string> = { available: 'Disponible', degraded: 'Dégradé', unavailable: 'Indisponible', unknown: 'Inconnu' }
const timeLabel = (value?: Date) => value ? new Intl.DateTimeFormat('fr-FR', { hour: '2-digit', minute: '2-digit', second: '2-digit' }).format(value) : '—'
const durationLabel = (seconds: number) => seconds < 60 ? `${seconds} s` : `${Math.floor(seconds / 60)} min ${seconds % 60} s`

export function CockpitPage() {
  const { snapshot, connectionStatus, lastUpdated, refresh } = useNovaState()
  const [brain, setBrain] = useState<ProjectBrainStatus>()
  useEffect(() => { const controller = new AbortController(); void getProjectBrain(controller.signal).then(setBrain).catch(() => setBrain(undefined)); return () => controller.abort() }, [lastUpdated])
  if (!snapshot && connectionStatus === 'connecting') return <section className="cockpit-page" aria-busy="true"><header className="cockpit-header"><div><p className="eyebrow">SUPERVISION LOCALE</p><h1>Cockpit</h1></div></header><div className="cockpit-grid"><div className="skeleton" /><div className="skeleton" /><div className="skeleton" /></div></section>
  if (!snapshot) return <section className="cockpit-page cockpit-offline"><p className="eyebrow">SUPERVISION LOCALE</p><h1>Cockpit hors connexion</h1><p>L’API locale Nova ne répond pas. Le cockpit réessaiera automatiquement.</p><button className="ghost-button" type="button" onClick={() => void refresh()}>Actualiser</button></section>
  const project = snapshot.project
  return <section className="cockpit-page">
    <header className="cockpit-header"><div><p className="eyebrow">SUPERVISION LOCALE</p><h1>Cockpit</h1></div><div className="cockpit-refresh"><span className={`connection-badge ${connectionStatus}`}>{connectionStatus === 'connected' ? 'Connecté' : 'Déconnecté'}</span><span>Mis à jour à {timeLabel(lastUpdated)}</span><button className="ghost-button" type="button" onClick={() => void refresh()}>Actualiser</button></div></header>
    {connectionStatus === 'disconnected' && <div className="stale-banner" role="status">Connexion interrompue — dernier instantané valide affiché.</div>}
    <div className="cockpit-grid cockpit-summary">
      <article className="cockpit-card accent"><span className="card-label">ÉTAT NOVA</span><strong>{snapshot.nova.label}</strong><p>{snapshot.nova.message}</p><small>{snapshot.nova.busy ? 'Activité en cours' : 'Disponible'}</small></article>
      <article className="cockpit-card success"><span className="card-label">API LOCALE</span><strong>Opérationnelle</strong><p>Durée de fonctionnement : {durationLabel(snapshot.api.uptime_seconds)}</p><small>Lecture seule</small></article>
      <article className={`cockpit-card ${project.git_available && project.clean ? 'success' : 'warning'}`}><span className="card-label">PROJET GIT</span><strong>{project.git_available ? (project.clean ? 'Dépôt propre' : 'Modifications présentes') : 'Git indisponible'}</strong><p>Branche : {project.branch ?? 'Inconnue'}</p><small>{project.git_available ? `${project.modified_count ?? 0} modifié(s) · ${project.untracked_count ?? 0} non suivi(s)` : 'Aucun détail disponible'}</small></article>
      <article className={`cockpit-card ${brain?.status === 'ready' ? 'success' : 'warning'}`}><span className="card-label">PROJECT BRAIN</span><strong>{brain?.status === 'ready' ? 'Index prêt' : 'Index indisponible'}</strong><p>{brain ? `${brain.file_count} fichiers · ${brain.symbol_count} symboles` : 'Aucune métadonnée disponible'}</p><small>{brain?.last_updated ? `Mis à jour : ${new Date(brain.last_updated).toLocaleTimeString('fr-FR')}` : 'Indexé à la demande'}</small></article>
    </div>
    <div className="cockpit-columns">
      <article className="cockpit-section"><h2>Composants</h2><div className="component-list">{snapshot.components.map((component) => <div className="component-row" key={component.id}><span className={`status-mark ${component.status}`} aria-hidden="true" /><div><strong>{component.label}</strong><small>{component.message}</small></div><span className={`status-text ${component.status}`}>{labels[component.status]}</span></div>)}</div></article>
      <article className="cockpit-section"><h2>Qualité et validations</h2><div className={`validation-state ${snapshot.validation.status}`}><span>{snapshot.validation.status === 'unknown' ? 'Inconnue' : snapshot.validation.status === 'passed' ? 'Réussie' : 'Échec'}</span><strong>{snapshot.validation.message}</strong></div><dl className="quality-counts"><div><dt>Réussis</dt><dd>{snapshot.validation.passed ?? '—'}</dd></div><div><dt>Échoués</dt><dd>{snapshot.validation.failed ?? '—'}</dd></div><div><dt>Avertissements</dt><dd>{snapshot.validation.warnings ?? '—'}</dd></div></dl></article>
    </div>
    <article className="cockpit-section alerts-section"><h2>Alertes</h2>{snapshot.alerts.length === 0 ? <p className="empty-state">Aucune alerte connue.</p> : <ul className="alert-list">{snapshot.alerts.map((alert, index) => <li className={alert.level} key={`${alert.message}-${index}`}><span>{alert.level}</span>{alert.message}</li>)}</ul>}</article>
  </section>
}
