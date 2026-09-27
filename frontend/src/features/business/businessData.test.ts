import assert from 'node:assert/strict'
import test from 'node:test'
import { blockedMarketDiagnostic, confidenceText, displayCell, evidenceText, formatPercent, formatTimestamp, instrumentFromDataRow, qualityText, safeText, sourceText } from './businessData.ts'

test('maps nested instrument, quality and source using backend schema', () => {
  const row = { instrument: { symbol: 'BTC-USD', display_name: 'BTC/USD' }, quality: { freshness_status: 'FRESH' }, source: { provider: 'coinbase' } }
  assert.equal(instrumentFromDataRow(row), 'BTC-USD')
  assert.equal(displayCell('instrument', row.instrument, row), 'BTC/USD')
  assert.equal(qualityText(row.quality), 'FRESH')
  assert.equal(sourceText(row.source), 'coinbase')
})

test('maps nested confidence and evidence without object coercion', () => {
  assert.equal(confidenceText({ value: .82 }), '+82.00%')
  assert.equal(evidenceText({ summary: 'Two corroborating feeds', count: 2 }), 'Two corroborating feeds')
  assert.equal(safeText({ unexpected: { nested: true } }), 'Unavailable')
  assert.notEqual(displayCell('unknown', {}, {}), '[object Object]')
})

test('formats timestamps and signed percentages', () => {
  assert.notEqual(formatTimestamp('2026-09-27T21:56:01Z'), '2026-09-27T21:56:01Z')
  assert.equal(formatTimestamp('invalid'), 'Unavailable')
  assert.equal(formatPercent(1.533), '+1.53%')
  assert.equal(formatPercent(-1.019), '-1.02%')
})

test('builds a backend-owned God Eyes blocked diagnostic', () => {
  assert.deepEqual(blockedMarketDiagnostic({ stale_quotes: 1, quote_count: 2, market_closed_quotes: 1, provider_errors: { coinbase: 'provider_failure' } }, {}), { title: 'ANALYSIS PAUSED', reason: 'Market data insufficiently fresh', detail: '1 / 2 monitored quotes stale · 1 market closed', providers: 'coinbase' })
  assert.equal(blockedMarketDiagnostic({ stale_quotes: 0, provider_errors: {} }, {}), undefined)
  assert.equal(blockedMarketDiagnostic({ stale_quotes: 0, provider_errors: {} }, {}, { status: 'blocked', blocked_reason: 'Insufficient verified history' })?.reason, 'Insufficient verified history')
})
