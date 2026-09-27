import { Icon } from './Icon'

interface SensitiveConfirmationProps { title: string; description: string; actionLabel: string }
export function SensitiveConfirmation({ title, description, actionLabel }: SensitiveConfirmationProps) {
  return <div className="confirmation-card"><div className="confirmation-icon"><Icon name="shield" /></div><div className="confirmation-content"><span className="confirmation-label">CONFIRMATION REQUISE · DÉMONSTRATION</span><strong>{title}</strong><p>{description}</p><div className="confirmation-actions"><button type="button" className="button-secondary" disabled>Annuler</button><button type="button" className="button-warning" disabled>{actionLabel}</button></div></div></div>
}
