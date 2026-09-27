// Real journal/SSE shape: no conversation_id and no generation_id are required.
export const goalAwaitingConfirmationEvent = {
  type: 'goal.awaiting_confirmation',
  goal_id: 'goal-confirmation-fixture',
} as const
