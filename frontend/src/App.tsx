import { useState } from 'react'
import { AppShell } from './app/AppShell'
import type { PageId } from './app/navigation'
import { NovaStateProvider } from './state/NovaStateProvider'
import './styles/global.css'
import './styles/business.css'
import './styles/qa.css'
import './styles/premium.css'

function App() {
  const [activePage, setActivePage] = useState<PageId>('overview')
  return <NovaStateProvider><AppShell activePage={activePage} onNavigate={setActivePage} /></NovaStateProvider>
}

export default App
