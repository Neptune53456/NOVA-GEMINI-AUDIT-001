import assert from 'node:assert/strict'
import test from 'node:test'
import { calibratedProbability, entryState, equityPoints, headerMode, portfolioSummary, positionRows, pricePoints, primaryRows, providerStatus, rankedOpportunities, rowsFor, statusTone, systemSummary } from './presentation.ts'

test('named lists cannot accidentally show equity observations as open positions', () => {
  assert.equal(positionRows({ equity_history: [{ equity: 100 }] }), undefined)
  assert.deepEqual(positionRows({ open_positions: [], equity_history: [{ equity: 100 }] }), [])
  assert.deepEqual(primaryRows({ errors: [{ error: 'source' }], quotes: [] }, 'quotes'), [])
})
test('separates closed positions and preserves unknown states for inspection', () => {
  const data = { positions: [{ status: 'OPEN' }, { status: 'CLOSED' }, { status: 'EXITED' }, { status: 'CANCELLED' }, { status: 'pending_review' }] }
  assert.deepEqual(positionRows(data), [{ status: 'OPEN' }, { status: 'pending_review' }])
  assert.deepEqual(positionRows(data, true), [{ status: 'CLOSED' }, { status: 'EXITED' }])
})
test('provider failure and market closure do not decide trader entry permissions', () => {
  assert.equal(entryState({ status: 'degraded', stale_quotes: 11 }), 'Entry status not reported')
  assert.equal(entryState({ entries_paused: true, mode: 'AUTO_PAPER' }), 'AUTO PAPER · NEW ENTRIES PAUSED')
  assert.equal(entryState({ entries_paused: false, mode: 'AUTO_PAPER' }), 'AUTO PAPER · ACTIVE')
  assert.equal(entryState({ mode: 'AUTO_PAPER' }), 'AUTO PAPER · Entry status not reported')
  assert.equal(entryState({ mode: 'OBSERVE' }), 'OBSERVE')
  assert.equal(entryState({ mode: 'ASSISTED_PAPER' }), 'ASSISTED PAPER')
  assert.equal(entryState({ trader: { entries_paused: false } }), 'Active')
  assert.equal(statusTone('MARKET_CLOSED'), '')
  assert.equal(statusTone('STALE'), 'warning-text')
  assert.equal(statusTone('DEGRADED'), 'warning-text')
  assert.equal(statusTone('FAILED'), 'negative')
})
test('provider wording keeps backend diagnostics separate from the default status', () => {
  assert.equal(providerStatus('ok'), 'Healthy')
  assert.equal(providerStatus('market_closed'), 'Market closed')
  assert.equal(providerStatus('degraded'), 'Degraded')
  assert.equal(providerStatus('unknown'), 'Not checked')
})
test('only valid explicitly calibrated probabilities are shown as percentages', () => {
  assert.equal(calibratedProbability({ raw_score: .99, confidence: .99 }), 'Not calibrated')
  assert.equal(calibratedProbability({ probability_up: .67, calibrated: false }), 'Not calibrated')
  assert.equal(calibratedProbability({ calibrated: true, probability_up: .67 }), '67.0% up')
  assert.equal(calibratedProbability({ calibrated: true, calibrated_probability: 0 }), '0.0%')
  assert.equal(calibratedProbability({ calibrated: true, probability_up: 67 }), 'Not calibrated')
  assert.equal(calibratedProbability({ calibrated: true, probability_up: NaN }), 'Not calibrated')
})
test('opportunity ordering follows explicit rank and never invents score ranking', () => {
  const rows = [{ rank: 3, star_score: 100 }, { rank: 1, star_score: 20 }]
  assert.equal(rankedOpportunities(rows)[0].rank, 1)
  assert.equal(rows[0].rank, 3)
  const unranked = [{ star_score: 1 }, { star_score: 99 }]
  assert.deepEqual(rankedOpportunities(unranked), unranked)
})
test('equity chart uses only timestamped finite observations, including zero', () => {
  assert.deepEqual(equityPoints({ equity: 100 }), [])
  assert.deepEqual(equityPoints({ equity_curve: [{ timestamp: 'bad', equity: 20 }, { timestamp: '2026-01-01', equity: 0 }, { timestamp: '2026-01-02', equity: Infinity }] }), [{ timestamp: Date.parse('2026-01-01'), value: 0 }])
})
test('portfolio summary preserves backend totals and never assumes missing positions are zero', () => {
  assert.equal(portfolioSummary({ equity: 0 }).equity, 0)
  assert.equal(portfolioSummary({ equity: 0 }).open_position_count, undefined)
  assert.equal(portfolioSummary({ open_positions: [] }).open_position_count, 0)
  assert.equal(portfolioSummary({ open_position_count: 5, open_positions: [] }).open_position_count, 5)
  assert.equal(portfolioSummary({ summary: { equity: 120 }, equity: 100 }).equity, 120)
  assert.equal(portfolioSummary({ portfolio: { equity: 120, cash: 80, realized_pnl: 2, unrealized_pnl: 3 } }).total_pnl, 5)
  assert.equal(portfolioSummary({ portfolio: { equity: 120 } }).total_pnl, undefined)
})

test('authoritative trader portfolio maps configured baseline and ledger metrics', () => {
  const response = { portfolio_id: 'NOVA_COMPOSITE', mode: 'OBSERVE', base_currency: 'USD', starting_capital: 10000,
    equity: 10020, cash: 9990, realized_pnl: 15, unrealized_pnl: 5, gross_exposure: 30,
    drawdown: 0.02, positions: [], paper_only: true }
  const summary = portfolioSummary(response)
  assert.equal(summary.equity, 10020)
  assert.equal(summary.cash, 9990)
  assert.equal(summary.total_pnl, 20)
  assert.ok(Math.abs(Number(summary.total_return_pct) - 0.2) < 1e-9)
  assert.equal(summary.exposure, 30)
  assert.equal(summary.drawdown_pct, 2)
  assert.equal(summary.open_position_count, 0)
  assert.equal(portfolioSummary({ equity: 10020 }).total_return_pct, undefined)
})

test('runtime mode honors the backend entry gate', () => {
  assert.equal(entryState({ mode: 'OBSERVE', new_entries_paused: false }), 'OBSERVE')
  assert.equal(entryState({ mode: 'AUTO_PAPER', new_entries_paused: false }), 'AUTO PAPER · ACTIVE')
  assert.equal(entryState({ mode: 'AUTO_PAPER', new_entries_paused: true }), 'AUTO PAPER · NEW ENTRIES PAUSED')
  assert.equal(headerMode({ mode: 'OBSERVE' }), 'PAPER · OBSERVE')
  assert.equal(headerMode({ mode: 'AUTO_PAPER', new_entries_paused: false }), 'AUTO PAPER · ACTIVE')
  assert.equal(headerMode({ mode: 'AUTO_PAPER', new_entries_paused: true }), 'AUTO PAPER · NEW ENTRIES PAUSED')
})

test('system status uses scheduler, retention, watchdog and provider evidence', () => {
  const health = { status: 'degraded', quote_count: 2, stale_quotes: 0, provider_health: [{ name: 'Coinbase', status: 'healthy' }, { name: 'IMF', status: 'degraded' }] }
  const endurance = { scheduler: { scheduler_alive: true, lanes: { critical: true, trading: true, ingestion: true }, tasks: { market: { lane: 'critical', last_status: 'success', last_finished_at: '2026-09-27T10:00:00Z' } } }, watchdog: { healthy: true, alerts: [] }, storage_cleanup: { healthy: true, last_cleanup: '2026-09-27T09:00:00Z', errors: [] }, backlogs: { closed_trades_not_resolved: 0 }, outcome_state: { pending: 0, resolved: 0, retry_pending: 0, exhausted: 0 } }
  const result = systemSummary(health, endurance, { mode: 'OBSERVE', paper_only: true })
  assert.equal((result.scheduler as { status: string }).status, 'Healthy')
  assert.equal((result.retention as { status: string }).status, 'Healthy')
  assert.equal((result.watchdog as { status: string }).status, 'Healthy')
  assert.equal((result.outcomes as { status: string }).status, 'Healthy')
  assert.equal((result.trader as { status: string }).status, 'Not Checked')
  assert.equal((systemSummary(health, endurance, { mode: 'OBSERVE', paper_only: true, kill_switch: false, broker: 'FakeSandboxBroker', risk: { state: 'normal' }, automation_enabled: false }).trader as { status: string }).status, 'Healthy')
  assert.equal(result.provider_health, health.provider_health)
  assert.equal(systemSummary(health).retention, undefined)
  assert.equal((systemSummary(health, { scheduler: { scheduler_alive: false, lanes: { critical: false } } }).scheduler as { status: string }).status, 'Degraded')
})
test('named strategy catalog includes zero-trade entries and object catalogs retain names', () => {
  const strategies = Array.from({ length: 6 }, (_, index) => ({ name: `TEST strategy ${index}`, trade_count: 0 }))
  assert.equal(primaryRows({ strategies, trades: [] }, 'strategies').length, 6)
  assert.deepEqual(rowsFor({ strategies: { momentum: { trade_count: 0 } } }, 'strategies'), [{ name: 'momentum', trade_count: 0 }])
})
test('sparklines require actual timestamped price history', () => {
  assert.deepEqual(pricePoints({ price: 100, change_percent: 2 }), [])
  assert.deepEqual(pricePoints({ price_history: [{ timestamp: '2026-01-02', price: 20 }, { timestamp: '2026-01-01', price: 10 }, { timestamp: 'invalid', price: 200 }] }).map(point => point.value), [10, 20])
})
