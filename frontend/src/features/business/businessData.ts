export type DataRow = Record<string, unknown>

export const isDataRow = (value: unknown): value is DataRow => typeof value === 'object' && value !== null && !Array.isArray(value)
export const dataRows = (data: DataRow): DataRow[] => {
  const rows = Object.values(data).find((value): value is unknown[] => Array.isArray(value) && value.every((item) => !item || isDataRow(item)))
  return rows?.filter(isDataRow) ?? []
}

const firstText = (...values: unknown[]) => values.find((value): value is string => typeof value === 'string' && value.trim().length > 0)
const compactId = (value: string) => value.length > 28 ? `${value.slice(0, 12)}…${value.slice(-8)}` : value

export function safeText(value: unknown, fallback = 'Unavailable'): string {
  if (value === null || value === undefined || value === '') return fallback
  if (typeof value === 'string') return value
  if (typeof value === 'number') return Number.isFinite(value) ? new Intl.NumberFormat('en-US', { maximumFractionDigits: 4 }).format(value) : fallback
  if (typeof value === 'boolean') return value ? 'Yes' : 'No'
  if (Array.isArray(value)) return value.length ? value.map((item) => safeText(item)).join(', ') : fallback
  if (!isDataRow(value)) return fallback
  return firstText(value.display_name, value.symbol, value.instrument_id, value.provider, value.name, value.level,
    value.status, value.summary, value.type, value.source, value.entity) ??
    (typeof value.value === 'number' ? safeText(value.value) : typeof value.score === 'number' ? safeText(value.score) : fallback)
}

export const instrumentText = (value: unknown) => isDataRow(value)
  ? firstText(value.display_name, value.symbol, value.instrument_id) ?? 'Unavailable' : safeText(value)
export const sourceText = (value: unknown) => isDataRow(value)
  ? firstText(value.provider, value.name, value.id, value.source_id) ?? 'Unavailable' : safeText(value)
export const qualityText = (value: unknown) => isDataRow(value)
  ? firstText(value.level, value.status, value.freshness_status) ?? (typeof value.score === 'number' ? safeText(value.score) : 'Unavailable') : safeText(value)
export const confidenceText = (value: unknown) => isDataRow(value)
  ? firstText(value.level) ?? (typeof value.value === 'number' ? formatPercent(value.value <= 1 ? value.value * 100 : value.value) : 'Unavailable') : safeText(value)
export const evidenceText = (value: unknown) => {
  if (Array.isArray(value)) return value.length ? `${value.length} item${value.length === 1 ? '' : 's'}` : 'Unavailable'
  if (!isDataRow(value)) return safeText(value)
  const summary = firstText(value.summary, value.type, value.source)
  return summary ?? (typeof value.count === 'number' ? `${value.count} item${value.count === 1 ? '' : 's'}` : 'Unavailable')
}

export function formatTimestamp(value: unknown): string {
  if (typeof value !== 'string') return 'Unavailable'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? 'Unavailable' : new Intl.DateTimeFormat(undefined, { dateStyle: 'short', timeStyle: 'medium' }).format(date)
}
export function formatAge(seconds: unknown): string {
  if (typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds < 0) return 'Unavailable'
  if (seconds < 60) return `${Math.round(seconds)}s ago`
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`
  return `${Math.round(seconds / 3600)}h ago`
}
export function formatPercent(value: unknown): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return 'Unavailable'
  return `${value > 0 ? '+' : ''}${value.toFixed(2)}%`
}
export function formatPrice(value: unknown, instrument: unknown): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return 'Unavailable'
  const currency = isDataRow(instrument) ? firstText(instrument.currency) : undefined
  if (!currency) return new Intl.NumberFormat('en-US', { maximumFractionDigits: 8 }).format(value)
  try { return new Intl.NumberFormat('en-US', { style: 'currency', currency, maximumFractionDigits: value < 1 ? 8 : 2 }).format(value) }
  catch { return new Intl.NumberFormat('en-US', { maximumFractionDigits: 8 }).format(value) }
}

export function displayCell(key: string, raw: unknown, row: DataRow): string {
  if (key === 'instrument') return instrumentText(raw)
  if (key === 'source') return sourceText(raw)
  if (key === 'quality') return qualityText(raw)
  if (key === 'confidence') return confidenceText(raw)
  if (key === 'evidence') return evidenceText(raw)
  if (key === 'entities') return Array.isArray(raw) ? raw.map(instrumentText).join(', ') || 'Unavailable' : instrumentText(raw)
  if (key === 'evidence_refs' || key.endsWith('_id') || key.endsWith('_hash')) return typeof raw === 'string' ? compactId(raw) : safeText(raw)
  if (key === 'price' || key === 'reference_price') return formatPrice(raw, row.instrument)
  if (key.includes('percent') || key.endsWith('_pct')) return formatPercent(raw)
  if (key === 'observed_at' || key === 'updated_at' || key === 'timestamp' || key.endsWith('_at')) return formatTimestamp(raw)
  if (key === 'age_seconds') return formatAge(raw)
  return safeText(raw)
}

export const instrumentFromDataRow = (row: DataRow) => {
  const value = row.instrument
  if (isDataRow(value)) return firstText(value.symbol, value.instrument_id)
  return firstText(value, row.symbol, Array.isArray(row.instruments) ? row.instruments[0] : undefined)
}

export function blockedMarketDiagnostic(health: DataRow, market: DataRow, scanner: DataRow = {}) {
  const quotes = dataRows(market)
  const stale = Number(health.stale_quotes ?? quotes.filter((quote) => isDataRow(quote.quality) && quote.quality.freshness_status === 'STALE').length)
  const closed = Number(health.market_closed_quotes ?? 0)
  const errors = isDataRow(health.provider_errors) ? Object.keys(health.provider_errors) : []
  const scannerBlocked = scanner.status === 'blocked' || scanner.status === 'paused'
  const scannerReason = firstText(scanner.blocked_reason, scanner.block_reason, scanner.reason,
    Array.isArray(scanner.reason_codes) ? scanner.reason_codes.filter((value): value is string => typeof value === 'string').join(', ') : undefined)
  if (!stale && !errors.length && !scannerBlocked) return undefined
  return { title: 'ANALYSIS PAUSED', reason: scannerReason ?? (stale ? 'Market data insufficiently fresh' : errors.length ? 'Provider health degraded' : 'Scanner blocked'), detail: `${stale} / ${Number(health.quote_count ?? quotes.length)} monitored quotes stale${closed ? ` · ${closed} market closed` : ''}`, providers: errors.join(', ') || 'Unavailable' }
}
