import { useEffect, useState } from 'react'
import { Icon } from '../../components/Icon'
import type { ConfirmationCard } from '../../api/novaApi'

const capabilityLabels: Record<string, string> = {
  'filesystem.write': 'Écrire un fichier',
  'filesystem.read': 'Lire un fichier',
  'git.status': 'Vérifier l’état Git',
}

const riskLabels: Record<string, string> = { low: 'Faible', medium: 'Moyen', high: 'Élevé', critical: 'Critique' }

function capabilityLabel(capabilityId: string) {
  const knownLabel = capabilityLabels[capabilityId]
  if (knownLabel) return knownLabel
  const words = capabilityId.replace(/[._-]+/g, ' ')
  return words.charAt(0).toUpperCase() + words.slice(1)
}

interface Props {
  confirmation: ConfirmationCard
  pending: boolean
  expired: boolean
  onDecision: (approved: boolean) => void
}

export function GoalConfirmationCard({ confirmation, pending, expired, onDecision }: Props) {
  const [secondsLeft, setSecondsLeft] = useState(() => Math.max(0, confirmation.expires_in_seconds))

  useEffect(() => {
    const startedAt = Date.now()
    const timer = window.setInterval(() => {
      const elapsed = Math.floor((Date.now() - startedAt) / 1_000)
      setSecondsLeft(Math.max(0, confirmation.expires_in_seconds - elapsed))
    }, 1_000)
    return () => window.clearInterval(timer)
  }, [confirmation])

  const minutes = Math.floor(secondsLeft / 60)
  const seconds = secondsLeft % 60
  const risk = confirmation.risk ? (riskLabels[confirmation.risk.toLowerCase()] ?? confirmation.risk) : 'Non précisé'
  const reversible = confirmation.reversible === undefined ? 'Non précisé' : confirmation.reversible ? 'Oui' : 'Non'

  return <section className={`confirmation-card${expired ? ' confirmation-expired' : ''}`} aria-labelledby="goal-confirmation-title">
    <div className="confirmation-icon"><Icon name="shield" /></div>
    <div className="confirmation-content">
      <div className="confirmation-heading"><span className="confirmation-label">CONFIRMATION REQUISE</span><small>Expire dans {minutes} min {seconds.toString().padStart(2, '0')} s</small></div>
      <strong id="goal-confirmation-title">{capabilityLabel(confirmation.capability_id)}</strong>
      <dl className="confirmation-details">
        <dt>Cible</dt><dd>{confirmation.path ?? 'Non précisée'}</dd>
        <dt>Effet</dt><dd>{confirmation.effect}</dd>
        <dt>Risque</dt><dd><span className={`risk-badge risk-${confirmation.risk?.toLowerCase() ?? 'unknown'}`}>{risk}</span></dd>
        <dt>Réversible</dt><dd>{reversible}</dd>
      </dl>
      {expired && <p className="confirmation-expiry" role="alert">Cette confirmation a expiré.</p>}
      <div className="confirmation-actions">
        <button className="button-secondary" type="button" disabled={pending || expired} onClick={() => onDecision(false)}>Refuser</button>
        <button className="button-warning" type="button" disabled={pending || expired} onClick={() => onDecision(true)}>{pending ? 'Traitement…' : 'Autoriser'}</button>
      </div>
    </div>
  </section>
}
