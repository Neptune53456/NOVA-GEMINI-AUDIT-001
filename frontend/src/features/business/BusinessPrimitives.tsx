import type { ReactNode } from 'react'
import { displayCell, formatPrice, safeText, type DataRow } from './businessData'
import { equityPoints, finite, humanize, pricePoints, rowsFor, statusTone } from './presentation'

export function EmptyState({ title, detail }: { title: string; detail?: string }) {
  return <div className="empty-business"><strong>{title}</strong>{detail && <small>{detail}</small>}</div>
}
export function Status({ value }: { value: unknown }) {
  return <span className={`status-text ${statusTone(value)}`}>{humanize(value, 'Not checked')}</span>
}
export function Details({ data, title = 'Technical details' }: { data: unknown; title?: string }) {
  return <details className="technical-details"><summary>{title}</summary><pre>{JSON.stringify(data, null, 2)}</pre></details>
}
export function Section({ title, aside, children }: { title: string; aside?: ReactNode; children: ReactNode }) {
  return <section className="business-panel"><div className="panel-heading"><h2>{title}</h2>{aside}</div>{children}</section>
}
export function Facts({ row, fields }: { row: DataRow; fields: [string, string][] }) {
  return <dl className="business-facts">{fields.map(([key, title]) => <div key={key}><dt>{title}</dt><dd className={typeof row[key] === 'number' && /pnl|return|change/.test(key) ? Number(row[key]) < 0 ? 'negative' : 'positive' : undefined}>{displayCell(key, row[key], row)}</dd></div>)}</dl>
}
export function EquityCurve({ data }: { data: DataRow }) {
  const points = equityPoints(data)
  if (points.length < 2) return <div className="curve-empty">Equity history will appear as observations accumulate.</div>
  const low = Math.min(...points.map(p => p.value)), high = Math.max(...points.map(p => p.value))
  const start = points[0].timestamp, duration = points.at(-1)!.timestamp - start
  const path = points.map(p => `${12 + (duration ? (p.timestamp - start) / duration : 0) * 576},${140 - (high === low ? .5 : (p.value - low) / (high - low)) * 120}`).join(' ')
  return <figure className="equity-figure"><figcaption><span>Equity history</span><span>{safeText(low)} — {safeText(high)}</span></figcaption><svg viewBox="0 0 600 160" role="img" aria-label={`Paper equity, ${points.length} observations, from ${safeText(points[0].value)} to ${safeText(points.at(-1)!.value)}`}><path d="M12 140H588" stroke="var(--stroke)"/><polyline points={path} fill="none" stroke="currentColor" strokeWidth="2" vectorEffect="non-scaling-stroke"/></svg><figcaption><span>{new Date(start).toLocaleDateString()}</span><span>{new Date(points.at(-1)!.timestamp).toLocaleDateString()}</span></figcaption></figure>
}
export function PortfolioHero({ data }: { data: DataRow }) {
  return <section className="portfolio-hero"><div><p className="eyebrow">Paper portfolio {typeof data.currency === 'string' && <span>· {data.currency}</span>}</p><div className="equity-value">{formatPrice(data.total_equity ?? data.equity, { currency: data.currency })}</div><Facts row={data} fields={[[ 'total_pnl', 'Total P&L' ], ['total_return_pct', 'Total return'], ['today_pnl', 'Today · P&L']]}/></div><EquityCurve data={data}/><div className="portfolio-secondary"><Facts row={data} fields={[[ 'open_position_count', 'Open positions' ], ['exposure', 'Exposure'], ['drawdown_pct', 'Drawdown'], ['cash', 'Available cash']]}/></div></section>
}
export function Sparkline({ data }: { data: DataRow }) {
  const points = pricePoints(data)
  if (points.length < 2) return null
  const min = Math.min(...points.map(point => point.value)), max = Math.max(...points.map(point => point.value))
  const start = points[0].timestamp, duration = points.at(-1)!.timestamp - start
  const path = points.map(point => `${2 + (duration ? (point.timestamp - start) / duration : 0) * 92},${26 - (max === min ? .5 : (point.value - min) / (max - min)) * 24}`).join(' ')
  return <svg className="price-sparkline" viewBox="0 0 96 30" role="img" aria-label={`Price history from ${safeText(points[0].value)} to ${safeText(points.at(-1)!.value)}`}><polyline points={path} fill="none" stroke="currentColor" strokeWidth="1.5"/></svg>
}
export function PerformanceAnalytics({ data }: { data: DataRow }) {
  return <Section title="Performance analytics"><div className="analytics-grid">{[['pnl_by_asset', 'P&L by asset'], ['strategy_contribution', 'Strategy contribution']].map(([key, title]) => {
    const rows = rowsFor(data, key).filter(row => finite(row.pnl))
    const max = Math.max(...rows.map(row => Math.abs(Number(row.pnl))), 1)
    return <div key={key}><h3>{title}</h3>{!rows.length ? <EmptyState title="Awaiting performance observations."/> : <div className="performance-bars">{rows.map((row, index) => <div key={index}><span>{safeText(row.asset ?? row.strategy ?? row.name)}</span><span className="bar-track"><i className={Number(row.pnl) < 0 ? 'loss' : 'gain'} style={{ width: `${Math.abs(Number(row.pnl)) / max * 100}%` }}/></span><strong className={Number(row.pnl) < 0 ? 'negative' : 'positive'}>{safeText(row.pnl)}</strong></div>)}</div>}</div>
  })}</div><Details data={data} title="Complete performance data"/></Section>
}
