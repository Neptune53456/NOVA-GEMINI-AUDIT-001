import assert from 'node:assert/strict'
import test from 'node:test'
import { generationErrorLabels, generationErrorMessage, projectsVerifiedSuccess, shouldProjectGenerationError } from './generationErrorProjection.ts'

test('projects machine-readable provider and anti-spin failures', () => {
  assert.match(generationErrorMessage('omniroute_timeout', 'generic'), /OmniRoute/)
  assert.match(generationErrorMessage('provider_invalid_request', 'generic'), /refusé/)
  assert.match(generationErrorMessage('repeated_observation', 'generic'), /boucle/)
})

test('keeps the safe backend fallback for unknown categories', () => {
  assert.equal(generationErrorMessage('unknown', 'Erreur sûre'), 'Erreur sûre')
})

test('keeps UTF-8 accents intact in status and error text', () => {
  const text = `${generationErrorLabels.timeout} échec vérifié annulé`
  for (const expected of ['échec', 'dépassé', 'vérifié', 'annulé']) assert.match(text, new RegExp(expected))
  assert.doesNotMatch(text, /Ã/)
})

test('does not let a later error overwrite terminal success', () => {
  assert.equal(shouldProjectGenerationError(true), false)
  assert.equal(shouldProjectGenerationError(false), true)
})

test('projects deterministic completion as verified success', () => {
  assert.equal(projectsVerifiedSuccess('completed_verified'), true)
  assert.equal(projectsVerifiedSuccess('completed_unverified'), false)
  assert.equal(projectsVerifiedSuccess(undefined), false)
})
