export type GoalUiStatus = 'awaiting_confirmation' | 'running' | 'verifying' | 'completed_verified' | 'cancelled' | 'failed' | 'blocked'

export interface GoalUiProjection {
  confirmationVisible: boolean
  presence: 'awaiting-confirmation' | 'acting' | 'success' | 'error' | 'idle'
  terminal: boolean
}

export function projectGoalUi(status: GoalUiStatus): GoalUiProjection {
  if (status === 'awaiting_confirmation') return { confirmationVisible: true, presence: 'awaiting-confirmation', terminal: false }
  if (status === 'running' || status === 'verifying') return { confirmationVisible: false, presence: 'acting', terminal: false }
  if (status === 'completed_verified') return { confirmationVisible: false, presence: 'success', terminal: true }
  if (status === 'cancelled') return { confirmationVisible: false, presence: 'idle', terminal: true }
  return { confirmationVisible: false, presence: 'error', terminal: true }
}
