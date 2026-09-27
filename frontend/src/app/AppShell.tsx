import { useState } from 'react'
import { Sidebar } from '../components/Sidebar'
import { TopBar } from '../components/TopBar'
import { GodEyesPage } from '../features/god-eyes/GodEyesPage'
import { SettingsPage } from '../features/settings/SettingsPage'
import { BusinessPage, OverviewPage } from '../features/business/BusinessPage'
import { InstrumentPage } from '../features/instrument/InstrumentPage'
import type { JsonRow } from '../features/instrument/marketData'
import type { PageId } from './navigation'
interface AppShellProps { activePage: PageId; onNavigate: (page: PageId) => void }
export function AppShell({ activePage, onNavigate }: AppShellProps) {
  const [selection,setSelection]=useState<{instrument:string;context:JsonRow}>();const navigate=(page:PageId)=>{setSelection(undefined);onNavigate(page)};const openInstrument=(instrument:string,context:JsonRow)=>setSelection({instrument,context})
  const content=selection?<InstrumentPage instrument={selection.instrument} context={selection.context} onBack={()=>setSelection(undefined)}/>:activePage==='overview'?<OverviewPage onOpenInstrument={openInstrument}/>:activePage==='god-eyes'?<GodEyesPage onOpenInstrument={openInstrument}/>:activePage==='settings'?<SettingsPage/>:<BusinessPage key={activePage} page={activePage} onOpenInstrument={openInstrument}/>
  return <div className="app-shell business-shell"><Sidebar activePage={activePage} onNavigate={navigate}/><div className="workspace"><TopBar onNavigate={navigate}/><main className="main-content">{content}</main></div></div>
}

