import { useEffect, useRef, useState } from 'react'
import { navigationItems, type PageId } from '../app/navigation'
import { useNovaState } from '../state/NovaStateContext'
import { useBusinessResource } from '../features/business/useBusinessResource'
import { headerMode } from '../features/business/presentation'
export function TopBar({ onNavigate }: { onNavigate: (page: PageId) => void }) {
  const { connectionStatus, lastUpdated } = useNovaState()
  const trader = useBusinessResource('trader/status')
  const dialog = useRef<HTMLDialogElement>(null)
  const [query, setQuery] = useState('')
  useEffect(() => {
    const listener = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault()
        if (dialog.current?.open) dialog.current.close()
        else dialog.current?.showModal()
      }
    }
    addEventListener('keydown', listener)
    return () => removeEventListener('keydown', listener)
  }, [])
  const matches = navigationItems.filter(item => `${item.label} ${item.id}`.toLowerCase().includes(query.toLowerCase()))
  const navigate = (page: PageId) => { onNavigate(page); dialog.current?.close(); setQuery('') }
  return <><header className="topbar"><strong className="paper-label">{headerMode(trader.data)}</strong><span className="top-separator"/><span className="top-status"><span className={`live-dot ${connectionStatus}`}/>{connectionStatus === 'connected' ? 'Connected' : connectionStatus === 'connecting' ? 'Connecting' : 'Disconnected'}</span><button className="command-trigger" onClick={() => dialog.current?.showModal()}>Go to a page <kbd>Ctrl / ⌘ K</kbd></button><div className="top-meta"><span>Last refresh <b>{lastUpdated ? lastUpdated.toLocaleTimeString() : 'Not yet available'}</b></span></div></header><dialog ref={dialog} className="page-search" aria-labelledby="page-search-title"><div className="drawer-head"><h2 id="page-search-title">Go to a page</h2><button aria-label="Close page search" onClick={() => dialog.current?.close()}>×</button></div><input aria-label="Search pages" placeholder="Search pages" value={query} onChange={e => setQuery(e.target.value)} onKeyDown={e => { if (e.key === 'Enter' && matches[0]) navigate(matches[0].id) }}/><div className="search-results">{matches.map(item => <button key={item.id} onClick={() => navigate(item.id)}>{item.label}<span>↗</span></button>)}{!matches.length && <p>No matching page.</p>}</div></dialog></>
}
