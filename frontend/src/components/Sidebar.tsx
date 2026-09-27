import { navigationItems, type PageId } from '../app/navigation'
import { useNovaState } from '../state/NovaStateContext'
import { Icon } from './Icon'
interface SidebarProps { activePage: PageId; onNavigate: (page: PageId) => void }
const groups: { label: string; pages: PageId[] }[] = [
  { label: 'Workspace', pages: ['overview'] },
  { label: 'Markets', pages: ['markets', 'intelligence'] },
  { label: 'Trading', pages: ['star-finder', 'portfolio', 'trader'] },
  { label: 'Research', pages: ['strategies', 'research'] },
  { label: 'System', pages: ['god-eyes', 'system', 'settings'] },
]
export function Sidebar({ activePage, onNavigate }: SidebarProps) {
  const { connectionStatus } = useNovaState()
  return <aside className="sidebar"><div className="brand"><span className="brand-mark">N</span><span>NOVA <small>BUSINESS</small></span></div><nav className="navigation" aria-label="Main navigation">{groups.map(group => <div className="nav-group" key={group.label}><span className="nav-group-label">{group.label}</span>{group.pages.map(id => { const item = navigationItems.find(item => item.id === id)!; return <button key={id} className={`nav-item ${activePage === id ? 'active' : ''}`} aria-current={activePage === id ? 'page' : undefined} onClick={() => onNavigate(id)} type="button" title={item.label}><Icon name={item.icon}/><span>{item.label}</span></button> })}</div>)}</nav><div className="sidebar-footer" data-connection={connectionStatus}><span className="status-dot"/><div><strong>PAPER ONLY</strong><small>{connectionStatus === 'connected' ? 'API connected' : connectionStatus === 'connecting' ? 'Connecting…' : 'API unavailable'}</small></div></div></aside>
}
