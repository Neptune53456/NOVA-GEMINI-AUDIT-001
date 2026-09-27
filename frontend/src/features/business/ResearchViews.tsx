import { isDataRow, safeText, type DataRow } from './businessData'
import { Details, EmptyState, Facts, Section, Status } from './BusinessPrimitives'
import { humanize, primaryRows } from './presentation'
import { useBusinessResource } from './useBusinessResource'

export function StrategiesView({ data, query }: { data: DataRow; query: string }) {
  const incumbent = useBusinessResource('incumbent')
  const rows = primaryRows(data, 'strategies', 'results').filter(row => JSON.stringify(row).toLowerCase().includes(query.toLowerCase()))
  const champion = isDataRow(data.champion) ? data.champion : isDataRow(incumbent.data?.incumbent) ? incumbent.data.incumbent : isDataRow(incumbent.data?.champion) ? incumbent.data.champion : undefined
  return <><div className="nova-brief"><span className="eyebrow">Active champion</span><h2>{champion ? humanize(champion.name ?? champion.strategy) : 'No champion reported'}</h2><p>{champion ? humanize(champion.reason, 'Selected by NOVA’s evaluation process.') : 'A champion requires sufficient validated evidence. No selection is reported here yet.'}</p><Details data={incumbent.data ?? incumbent.error ?? "Loading champion state"} title="Champion selection evidence"/></div><Section title="Strategies & challengers">{!rows.length ? <EmptyState title={query ? "No strategies match your search." : "Awaiting strategy observations."} detail="The strategy catalog has not been supplied by this endpoint."/> : <div className="strategy-list">{rows.map((row, i) => <article key={i}><div><h3>{humanize(row.name ?? row.strategy ?? row.strategy_name, 'Strategy')}</h3><Status value={row.role ?? row.status}/></div><Facts row={row} fields={[[ 'trade_count', 'Trades' ], ['return_pct', 'Return'], ['drawdown_pct', 'Drawdown']]}/>{(row.trade_count === 0 || row.observations === 0) && <p>Awaiting observations.</p>}<Details data={row} title="Performance & validation evidence"/></article>)}</div>}</Section></>
}
export function ResearchView({ data, query }: { data: DataRow; query: string }) {
  const rows = primaryRows(data, 'experiments', 'candidates').filter(row => JSON.stringify(row).toLowerCase().includes(query.toLowerCase()))
  return <><ol className="research-pipeline" aria-label="Research evaluation stages">{['Candidate', 'Replay', 'Holdout', 'Forward validation', 'Judge', 'Promotion'].map((stage, i) => <li key={stage}><span>{String(i + 1).padStart(2, '0')}</span>{stage}</li>)}</ol><p className="section-note">Evaluation stages · experiment progress is reported below.</p><Section title="Current experiments">{!rows.length ? <EmptyState title={query ? "No experiments match your search." : "No experiments reported yet."} detail="Research decisions will appear when evaluations are recorded."/> : <div className="strategy-list">{rows.map((row, i) => <article key={i}><div><h3>{safeText(row.name ?? row.strategy ?? row.title, 'Research candidate')}</h3><Status value={row.status}/></div><p>{humanize(row.reason ?? row.rejection_reason ?? row.promotion_reason, 'No decision explanation reported yet.')}</p><Facts row={row} fields={[[ 'stage', 'Current stage' ], ['decision', 'Judge decision']]}/><Details data={row} title="Advanced research inspector"/></article>)}</div>}</Section></>
}


