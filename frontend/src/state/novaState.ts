export type { NovaState, NovaStateResponse } from '../api/novaApi'
import type { NovaState } from '../api/novaApi'

export const novaStateLabels: Record<NovaState, string> = {
  idle: 'Disponible', listening: 'À votre écoute', thinking: 'Réflexion en cours', responding: 'Réponse en cours', acting: 'Action en cours',
  'awaiting-confirmation': 'Confirmation requise', success: 'Terminé', error: 'Attention requise',
  'self-improving': 'Auto-amélioration',
}
