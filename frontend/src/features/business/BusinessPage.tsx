import { useState, type ReactNode } from 'react'
import { type GodEyesEndpoint } from '../../api/novaApi'
import type { PageId } from '../../app/navigation'
import { dataRows, isDataRow, type DataRow } from './businessData'
import { Details, EmptyState, PerformanceAnalytics, PortfolioHero, Section, Status } from './BusinessPrimitives'
import { ActivityTimeline, IntelligenceFeed, MarketScreener, OpportunityBoard, PositionList, SystemHealth, type OpenInstrument } from './BusinessViews'
import { entryState, humanize, portfolioSummary, positionRows, primaryRows, rowsFor, systemSummary } from './presentation'
import { ResearchView, StrategiesView } from './ResearchViews'
import { useBusinessResource, type BusinessResource } from './useBusinessResource'

type BusinessId = Exclude<PageId, 'overview' | 'god-eyes' | 'settings'>
const configs: Record<BusinessId, { title: string; endpoint: GodEyesEndpoint; description: string }> = {
  markets: { title: 'Markets', endpoint: 'market', description: 'The markets NOVA is watching. Price, direction and data quality at a glance.' },
  intelligence: { title: 'Intelligence', endpoint: 'events', description: 'What happened, why it matters and the evidence behind it.' },
  'star-finder': { title: 'Opportunities', endpoint: 'opportunities', description: 'The shortlist. Qualified signals, after costs and risk.' },
  trader: { title: 'NOVA Trader', endpoint: 'portfolio', description: 'Current decisions and the paper trade lifecycle.' },
  portfolio: { title: 'Portfolio', endpoint: 'portfolio', description: 'Your simulated capital, open positions and realized results.' },
  strategies: { title: 'Strategies', endpoint: 'walk-forward', description: 'Evidence and performance across NOVA’s strategies.' },
  research: { title: 'Research Lab', endpoint: 'experiments', description: 'Follow the evidence from candidate to promotion.' },
  system: { title: 'System', endpoint: 'health', description: 'Data health, services and diagnostics.' },
}
export function ResourceView({ resource, children }: { resource: BusinessResource; children: (data: DataRow) => ReactNode }) {
  return resource.error ? <ErrorState message={resource.error}/> : resource.data ? children(resource.data) : <Skeleton/>
}
export function DataPanel({ data, empty = 'No observations yet.', emptyDetail, onOpenInstrument }: { data: DataRow; empty?: string; emptyDetail?: string; onOpenInstrument?: OpenInstrument }) {
  const rows = dataRows(data)
  return rows.length ? <><IntelligenceFeed rows={rows} onOpen={onOpenInstrument}/><Details data={data} title="Complete backend record"/></> : <><EmptyState title={empty} detail={emptyDetail}/><Details data={data}/></>
}
export function BusinessPage({ page, onOpenInstrument }: { page: BusinessId; onOpenInstrument: OpenInstrument }) {
  const config = configs[page]
  const resource = useBusinessResource(config.endpoint)
  const [query, setQuery] = useState('')
  const search = (rows: DataRow[]) => rows.filter(row => JSON.stringify(row).toLowerCase().includes(query.toLowerCase()))
  return <section className="business-page"><PageHeader kicker="NOVA BUSINESS" title={config.title} description={config.description}/>
    {!['system', 'portfolio', 'trader'].includes(page) && <div className="toolbar"><label><span aria-hidden="true">⌕</span><input value={query} onChange={e => setQuery(e.target.value)} placeholder={`Search ${config.title.toLowerCase()}`} aria-label={`Search ${config.title}`}/></label></div>}
    <ResourceView resource={resource}>{data => <>
      {page === 'markets' && <Section title="Market screener"><MarketScreener searching={Boolean(query)} rows={search(primaryRows(data, 'quotes', 'markets'))} onOpen={onOpenInstrument}/></Section>}
      {page === 'intelligence' && <Section title="Latest intelligence"><IntelligenceFeed searching={Boolean(query)} rows={search(primaryRows(data, 'events'))} onOpen={onOpenInstrument}/></Section>}
      {page === 'star-finder' && <Section title="Opportunity board"><OpportunityBoard searching={Boolean(query)} rows={search(primaryRows(data, 'opportunities'))} onOpen={onOpenInstrument}/></Section>}
      {(page === 'portfolio' || page === 'trader') && <>
        {page === 'portfolio' ? <PortfolioHero data={portfolioSummary(data)}/> : <div className="nova-brief"><span className="eyebrow">PAPER</span><h2>{entryState(data)}</h2><p>{humanize(data.pause_reason ?? data.reason, "Decisions reflect the latest reported trader state.")}</p></div>}
        {page === 'trader' && <Section title="Current decisions"><ActivityTimeline rows={rowsFor(data, 'decisions')}/></Section>}
        <Section title="Open positions"><PositionList rows={positionRows(data)} onOpen={onOpenInstrument}/></Section>
        <Section title={page === 'portfolio' ? 'Recent closed trades' : 'Activity timeline'}>{page === 'portfolio' ? <PositionList rows={positionRows(data, true)} onOpen={onOpenInstrument} closed/> : <ActivityTimeline rows={rowsFor(data, 'timeline', 'activity', 'trades')}/>}</Section>
        {page === 'portfolio' && <PerformanceAnalytics data={isDataRow(data.analytics) ? data.analytics : isDataRow(data.performance) ? data.performance : data}/>}
      </>}
      {page === 'strategies' && <StrategiesView data={data} query={query}/>}
      {page === 'research' && <ResearchView data={data} query={query}/>}
      {page === 'system' && <SystemTelemetry health={data}/>}
      <Details data={data} title="Advanced · complete source data"/>
    </>}</ResourceView>
  </section>
}
function SystemTelemetry({ health }: { health: DataRow }) {
  const endurance = useBusinessResource('endurance/health')
  const trader = useBusinessResource('trader/status')
  return <SystemHealth data={systemSummary(health, endurance.data, trader.data)}/>
}
export function PageHeader({ kicker, title, description }: { kicker: string; title: string; description: string }) {
  return <header className="business-header"><div><p className="eyebrow">{kicker}</p><h1>{title}</h1><p>{description}</p></div></header>
}
export function ErrorState({ message }: { message: string }) {
  return <div className="error-business" role="alert"><strong>Data unavailable</strong><span>{message}</span><small>Refresh the page to try again.</small></div>
}
export function Skeleton() { return <div className="business-skeleton" role="status" aria-label="Loading data"><i/><i/><i/></div> }

export function OverviewPage({ onOpenInstrument }: { onOpenInstrument: OpenInstrument }) {
  const market = useBusinessResource('market'), opportunities = useBusinessResource('opportunities'), portfolio = useBusinessResource('portfolio'), health = useBusinessResource('health')
  const opportunityRows = opportunities.data ? primaryRows(opportunities.data, 'opportunities') : undefined
  const marketRows = market.data ? primaryRows(market.data, 'quotes', 'markets') : undefined
  return <section className="business-page overview"><PageHeader kicker="NOVA BUSINESS" title="Overview" description="Your paper portfolio. NOVA’s view of the market."/>
    <div className="overview-status"><div>System <Status value={health.error ? 'unavailable' : health.data?.status}/></div><span>{portfolio.data ? entryState(portfolio.data) : portfolio.error ? 'Trader unavailable' : 'Checking trader state…'}</span></div>
    <ResourceView resource={portfolio}>{data => <PortfolioHero data={portfolioSummary(data)}/>}</ResourceView>
    <div className="nova-brief"><span className="eyebrow">NOVA now</span><p>{health.data?.status === 'degraded' ? 'Market analysis is partially degraded. Review data health before interpreting signals.' : marketRows ? `NOVA is monitoring ${marketRows.length} markets. ${opportunityRows ? opportunityRows.length ? `${opportunityRows.length} opportunities reported for review.` : 'No opportunity currently meets the entry requirements.' : 'Opportunity status is not available yet.'}` : 'Waiting for the latest market state.'}</p><details><summary>View details</summary><ResourceView resource={health}>{data => <SystemHealth data={data}/>}</ResourceView></details></div>
    <div className="overview-grid"><Section title="Top opportunities" aside={<span className="section-note">Backend ranking</span>}><ResourceView resource={opportunities}>{data => <OpportunityBoard rows={primaryRows(data, 'opportunities')} onOpen={onOpenInstrument} limit={3}/>}</ResourceView></Section><Section title="Open positions"><ResourceView resource={portfolio}>{data => <PositionList rows={positionRows(data)} onOpen={onOpenInstrument}/>}</ResourceView></Section></div>
    <Section title="Market pulse"><ResourceView resource={market}>{data => <MarketScreener rows={primaryRows(data, 'quotes', 'markets')} onOpen={onOpenInstrument} compact/>}</ResourceView></Section>
  </section>
}




