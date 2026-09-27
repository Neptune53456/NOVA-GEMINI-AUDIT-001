export type PageId = 'overview' | 'god-eyes' | 'markets' | 'intelligence' | 'star-finder' | 'trader' | 'portfolio' | 'strategies' | 'research' | 'system' | 'settings'
export interface NavigationItem { id: PageId; label: string; icon: string }
export const navigationItems: NavigationItem[] = [
  { id: 'overview', label: 'Overview', icon: 'pulse' }, { id: 'god-eyes', label: 'God Eyes', icon: 'pulse' },
  { id: 'markets', label: 'Markets', icon: 'chart' }, { id: 'intelligence', label: 'Intelligence', icon: 'spark' },
  { id: 'star-finder', label: 'Opportunities', icon: 'star' }, { id: 'trader', label: 'Trader', icon: 'trade' },
  { id: 'portfolio', label: 'Portfolio', icon: 'portfolio' }, { id: 'strategies', label: 'Strategies', icon: 'layers' },
  { id: 'research', label: 'Research Lab', icon: 'model' }, { id: 'system', label: 'System', icon: 'shield' },
  { id: 'settings', label: 'Settings', icon: 'settings' },
]
