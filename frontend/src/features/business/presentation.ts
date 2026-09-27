import { dataRows, isDataRow, safeText, type DataRow } from './businessData.ts'

const explanations: Record<string, string> = {
  insufficient_samples: 'More historical observations are needed.',
  insufficient_history: 'Not enough market history is available yet.',
  market_closed: 'Market closed',
  stale_data: 'Market data is no longer fresh enough.',
  provider_unavailable: 'A data source is currently unavailable.',
}
export const humanize = (value: unknown, fallback = 'Not reported') => {
  const text = safeText(value, fallback)
  return explanations[text.toLowerCase()] ?? text.replaceAll('_', ' ')
}
export function rowsFor(data: DataRow, ...keys: string[]): DataRow[] {
  for (const key of keys) {
    const value = data[key]
    if (Array.isArray(value)) return value.filter(isDataRow)
    if (isDataRow(value)) return Object.entries(value).flatMap(([name, item]) => isDataRow(item) ? [{ name, ...item }] : [])
  }
  return []
}
export function primaryRows(data: DataRow, ...keys: string[]) {
  return keys.some(key => key in data) ? rowsFor(data, ...keys) : dataRows(data)
}
export const finite = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value)
export const opportunityState = (row: DataRow) => String(row.qualification_status ?? row.status ?? row.decision ?? 'not reported').toLowerCase()
export function rankedOpportunities(rows: DataRow[]) {
  // Preserve the backend ranking unless it supplies an explicit rank.
  return rows.every(row => finite(row.rank)) ? [...rows].sort((a, b) => Number(a.rank) - Number(b.rank)) : [...rows]
}
export function calibratedProbability(row: DataRow) {
  if (row.calibrated === false) return 'Not calibrated'
  if (row.calibrated !== true && row.calibration_status !== 'calibrated' && row.calibration_status !== 'CALIBRATED') return 'Not calibrated'
  const value = row.calibrated_probability ?? row.probability_up ?? row.probability_down
  return finite(value) && value >= 0 && value <= 1 ? `${(value * 100).toFixed(1)}%${row.calibrated_probability === undefined ? row.probability_up !== undefined ? ' up' : ' down' : ''}` : 'Not calibrated'
}
export function entryState(data: DataRow) {
  const trader = isDataRow(data.trader) ? data.trader : data
  const mode = String(trader.mode ?? data.mode ?? '').toUpperCase()
  const status = String(trader.entry_status ?? trader.status ?? '').toUpperCase()
  if (mode === 'OBSERVE' || mode === 'ASSISTED_PAPER') return mode.replace('_', ' ')
  if (mode === 'AUTO_PAPER') {
    if (trader.new_entries_paused === true || trader.entries_paused === true || ['PAUSED', 'ENTRIES_PAUSED', 'BLOCKED'].includes(status)) return 'AUTO PAPER · NEW ENTRIES PAUSED'
    if (trader.new_entries_paused === false || trader.entries_paused === false || status === 'ACTIVE') return 'AUTO PAPER · ACTIVE'
    return 'AUTO PAPER · Entry status not reported'
  }
  if (trader.new_entries_paused === true || trader.entries_paused === true || ['PAUSED', 'ENTRIES_PAUSED'].includes(status)) return 'Entries paused'
  if (trader.new_entries_paused === false || trader.entries_paused === false || status === 'ACTIVE') return 'Active'
  return 'Entry status not reported'
}
export function positionRows(data: DataRow, closed = false): DataRow[] | undefined {
  const key = closed ? 'closed_positions' : 'open_positions'
  if (Array.isArray(data[key])) return rowsFor(data, key)
  if (closed && Array.isArray(data.closed_trades)) return rowsFor(data, 'closed_trades')
  if (!Array.isArray(data.positions)) return undefined
  return rowsFor(data, 'positions').filter(row => {
    const status = String(row.status ?? '').toLowerCase()
    if (closed) return ['closed', 'exited'].includes(status)
    return !['closed', 'exited', 'cancelled', 'rejected'].includes(status)
  })
}
export function portfolioSummary(data: DataRow): DataRow {
  const ledger = isDataRow(data.portfolio) ? data.portfolio : data
  const summary = isDataRow(ledger.summary) ? { ...ledger, ...ledger.summary } : ledger
  const positions = positionRows(data)
  const realized = finite(summary.realized_pnl) ? summary.realized_pnl : undefined
  const unrealized = finite(summary.unrealized_pnl) ? summary.unrealized_pnl : undefined
  return { ...summary,
    currency: summary.currency ?? summary.base_currency,
    exposure: summary.exposure ?? summary.gross_exposure,
    drawdown_pct: summary.drawdown_pct ?? (finite(summary.drawdown) ? summary.drawdown * 100 : undefined),
    total_return_pct: summary.total_return_pct ?? (finite(summary.equity) && finite(summary.starting_capital) && summary.starting_capital > 0 ? (summary.equity / summary.starting_capital - 1) * 100 : undefined),
    total_pnl: summary.total_pnl ?? (realized !== undefined && unrealized !== undefined ? realized + unrealized : undefined),
    open_position_count: summary.open_position_count ?? positions?.length,
  }
}
export function headerMode(data?: DataRow) {
  if (!data) return 'PAPER · MODE UNAVAILABLE'
  const state = entryState(data)
  return state.startsWith('AUTO PAPER') ? state : `PAPER · ${state}`
}
export function systemSummary(health: DataRow, endurance?: DataRow, trader?: DataRow): DataRow {
  const scheduler = isDataRow(endurance?.scheduler) ? endurance.scheduler : isDataRow(health.scheduler) ? health.scheduler : undefined
  const lanes = scheduler && isDataRow(scheduler.lanes) ? scheduler.lanes : undefined
  const retention = isDataRow(endurance?.storage_cleanup) ? endurance.storage_cleanup : undefined
  const watchdog = isDataRow(endurance?.watchdog) ? endurance.watchdog : undefined
  const backlogs = isDataRow(endurance?.backlogs) ? endurance.backlogs : undefined
  const outcomeState = isDataRow(endurance?.outcome_state) ? endurance.outcome_state : undefined
  const tasks = scheduler && isDataRow(scheduler.tasks) ? scheduler.tasks : undefined
  const laneStatuses = lanes ? ['critical', 'trading', 'ingestion'].map(name => lanes[name]) : []
  return { ...health,
    market_data: { status: finite(health.quote_count) ? health.stale_quotes || health.provider_unavailable_quotes ? 'Degraded' : health.quote_count > 0 ? 'Healthy' : 'Not Checked' : 'Not Checked', quote_count: health.quote_count, stale_quotes: health.stale_quotes },
    trader: trader ? { ...trader, status: trader.kill_switch === true || trader.paper_only === false || trader.risk && isDataRow(trader.risk) && trader.risk.state !== 'normal' ? 'Degraded' : trader.paper_only === true && trader.broker === 'FakeSandboxBroker' && ['OBSERVE', 'ASSISTED_PAPER', 'AUTO_PAPER'].includes(String(trader.mode)) && trader.kill_switch === false ? 'Healthy' : 'Not Checked' } : undefined,
    scheduler: scheduler ? { ...scheduler, status: scheduler.scheduler_alive === false || laneStatuses.some(value => value === false) ? 'Degraded' : scheduler.scheduler_alive === true && laneStatuses.length === 3 && laneStatuses.every(value => value === true) ? 'Healthy' : 'Not Checked', lanes, tasks } : undefined,
    storage: isDataRow(health.storage) ? health.storage : undefined,
    retention: retention ? { ...retention, status: retention.last_cleanup ? retention.healthy === true ? 'Healthy' : 'Degraded' : 'Not Checked' } : undefined,
    watchdog: watchdog ? { ...watchdog, status: watchdog.healthy === true ? 'Healthy' : watchdog.healthy === false ? 'Degraded' : 'Not Checked' } : undefined,
    outcomes: outcomeState ? { ...outcomeState, status: finite(outcomeState.exhausted) && outcomeState.exhausted > 0 ? 'Degraded' : finite(outcomeState.pending) && outcomeState.pending > 0 ? 'Degraded' : 'Healthy', backlogs } : backlogs ? { status: finite(backlogs.closed_trades_not_resolved) && backlogs.closed_trades_not_resolved > 0 ? 'Degraded' : 'Not Checked', backlogs } : undefined,
  }
}
export function providerStatus(value: unknown): string {
  const status = String(value ?? '').toLowerCase()
  if (['ok', 'healthy', 'available', 'fresh'].includes(status)) return 'Healthy'
  if (['degraded', 'stale', 'error', 'failed'].includes(status)) return 'Degraded'
  if (status === 'market_closed') return 'Market closed'
  if (status === 'unavailable') return 'Unavailable'
  return 'Not checked'
}
export function statusTone(value: unknown) {
  const status = String(value ?? '').toLowerCase()
  if (['healthy', 'fresh', 'active', 'qualified', 'operational'].includes(status)) return 'positive'
  if (['degraded', 'stale', 'paused', 'watching'].includes(status)) return 'warning-text'
  if (['failed', 'unavailable', 'error', 'rejected'].includes(status)) return 'negative'
  return ''
}
export function equityPoints(data: DataRow) {
  // No inferred history, interpolation, or synthetic baseline.
  return rowsFor(data, 'equity_curve', 'equity_history').flatMap(row => {
    const timestamp = typeof row.timestamp === 'string' ? Date.parse(row.timestamp) : NaN
    return finite(row.equity) && Number.isFinite(timestamp) ? [{ timestamp, value: row.equity }] : []
  }).sort((a, b) => a.timestamp - b.timestamp)
}
export function pricePoints(data: DataRow) {
  return rowsFor(data, 'price_history').flatMap(row => {
    const timestamp = typeof row.timestamp === 'string' ? Date.parse(row.timestamp) : NaN
    return finite(row.price) && Number.isFinite(timestamp) ? [{ timestamp, value: row.price }] : []
  }).sort((a, b) => a.timestamp - b.timestamp)
}
