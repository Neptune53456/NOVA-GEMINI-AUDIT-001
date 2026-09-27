import { useEffect, useState } from 'react'
import { getGodEyes } from '../../api/novaApi'
import { PageHeader, ResourceView } from '../business/BusinessPage'
import { useBusinessResource } from '../business/useBusinessResource'
import { Details, Section } from '../business/BusinessPrimitives'
import { IntelligenceFeed, MarketScreener, OpportunityBoard, SystemHealth, type OpenInstrument } from '../business/BusinessViews'
import { primaryRows } from '../business/presentation'
import type { DataRow } from '../business/businessData'

const advancedTabs = ['Forecasts', 'Paper Portfolio', 'Evaluation', 'Research Lab', 'Alerts', 'Governance', 'Scanner'] as const
const endpoints: Record<typeof advancedTabs[number], string> = { 'Paper Portfolio': 'portfolio', Scanner: 'scanner', Forecasts: 'forecasts', Evaluation: 'walk-forward', 'Research Lab': 'experiments', Alerts: 'alerts', Governance: 'governance' }
export function GodEyesPage({ onOpenInstrument }: { onOpenInstrument: OpenInstrument }) {
  const market = useBusinessResource('market'), events = useBusinessResource('events'), opportunities = useBusinessResource('opportunities'), health = useBusinessResource('health')
  const [tab, setTab] = useState<typeof advancedTabs[number]>('Forecasts')
  const [advanced, setAdvanced] = useState<{ tab?: string; data?: DataRow; error?: string }>({})
  const [showAdvanced, setShowAdvanced] = useState(false)
  useEffect(() => {
    if (!showAdvanced) return
    const controller = new AbortController()
    getGodEyes(endpoints[tab], controller.signal).then(data => { if (!controller.signal.aborted) setAdvanced({ tab, data }) }).catch(error => { if (!controller.signal.aborted) setAdvanced({ tab, error: String(error) }) })
    return () => controller.abort()
  }, [tab, showAdvanced])
  return <section className="business-page"><PageHeader kicker="GOD EYES" title="What NOVA sees now" description="Market pulse, important events and active signals."/>
    <Section title="Market pulse"><ResourceView resource={market}>{data => <MarketScreener onOpen={onOpenInstrument} rows={primaryRows(data, 'quotes', 'markets')} compact/>}</ResourceView></Section>
    <Section title="Important events"><ResourceView resource={events}>{data => <IntelligenceFeed onOpen={onOpenInstrument} rows={primaryRows(data, 'events')}/>}</ResourceView></Section>
    <Section title="Active signals"><ResourceView resource={opportunities}>{data => <OpportunityBoard onOpen={onOpenInstrument} rows={primaryRows(data, 'opportunities')} limit={5}/>}</ResourceView></Section>
    <details className="diagnostics-disclosure"><summary>Data health</summary><ResourceView resource={health}>{data => <SystemHealth data={data}/>}</ResourceView></details>
    <details className="diagnostics-disclosure" onToggle={event => setShowAdvanced(event.currentTarget.open)}><summary>Advanced intelligence & governance</summary><nav className="filter-tabs" aria-label="Advanced God Eyes views">{advancedTabs.map(value => <button key={value} aria-pressed={tab === value} onClick={() => setTab(value)}>{value}</button>)}</nav><ResourceView resource={advanced.tab === tab ? advanced : {}}>{data => <Details data={data} title={`${tab} · complete evidence`}/>}</ResourceView></details>
  </section>
}



