export const generationErrorLabels: Readonly<Record<string, string>> = {
  omniroute_timeout: 'OmniRoute n’a pas répondu dans le délai imparti.',
  provider_invalid_request: 'Le fournisseur a refusé la requête envoyée par Nova.',
  provider_unavailable: 'Aucun fournisseur compatible n’est actuellement disponible.',
  no_tool_capable_provider: 'Aucun fournisseur capable d’utiliser les outils n’est disponible.',
  tool_protocol_error: 'La réponse outil du fournisseur était invalide.',
  capability_not_allowed: 'La capacité demandée n’est pas autorisée.',
  invalid_arguments: 'Les arguments de l’action sont invalides.',
  repeated_observation: 'Nova a interrompu une boucle sans nouvelle observation.',
  timeout: 'La demande a dépassé le délai autorisé.',
  action_budget_exceeded: 'La limite d’actions autorisées a été atteinte.',
  discovery_budget_exceeded: 'La limite d’observations autorisées a été atteinte.',
  model_turn_budget_exceeded: 'La limite de tours du modèle a été atteinte.',
}

export function shouldProjectGenerationError(terminalSuccess: boolean): boolean {
  return !terminalSuccess
}

export function projectsVerifiedSuccess(completionState: string | undefined): boolean {
  return completionState === 'completed_verified'
}

export function generationErrorMessage(code: string | undefined, fallback: string): string {
  return (code && generationErrorLabels[code]) || fallback
}
