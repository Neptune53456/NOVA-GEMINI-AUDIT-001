import assert from 'node:assert/strict'
import test from 'node:test'
import { projectGoalUi, type GoalUiStatus } from './goalStateProjection.ts'

test('confirmation is cleared throughout successful execution', () => {
  const sequence: GoalUiStatus[] = ['awaiting_confirmation', 'running', 'verifying', 'completed_verified']
  const projected = sequence.map(projectGoalUi)
  assert.equal(projected[0].confirmationVisible, true)
  assert.deepEqual(projected.slice(1).map((item) => item.confirmationVisible), [false, false, false])
  assert.deepEqual(projected.map((item) => item.presence), ['awaiting-confirmation', 'acting', 'acting', 'success'])
})

test('confirmation is cleared for every non-success terminal state', () => {
  for (const status of ['cancelled', 'failed', 'blocked'] as const) {
    const projected = projectGoalUi(status)
    assert.equal(projected.confirmationVisible, false)
    assert.equal(projected.terminal, true)
    assert.notEqual(projected.presence, 'awaiting-confirmation')
  }
})
