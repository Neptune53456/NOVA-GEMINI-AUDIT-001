import type { ReactNode } from 'react'
interface IconProps { name: string; size?: number }
const paths: Record<string, ReactNode> = {
  chat: <><path d="M4 5.5A2.5 2.5 0 0 1 6.5 3h11A2.5 2.5 0 0 1 20 5.5v7a2.5 2.5 0 0 1-2.5 2.5H10l-5 4v-4.5A2.5 2.5 0 0 1 4 12.5z" /><path d="M8 8h8M8 11h5" /></>,
  document: <><path d="M6 3h8l4 4v14H6z" /><path d="M14 3v5h4M9 12h6M9 16h6" /></>,
  model: <><circle cx="12" cy="12" r="3" /><path d="M12 2v4M12 18v4M2 12h4M18 12h4M5 5l3 3M16 16l3 3M19 5l-3 3M8 16l-3 3" /></>,
  flag: <><path d="M5 21V4" /><path d="M5 5h11l-2 4 2 4H5" /></>,
  spark: <><path d="m12 2 1.5 5.5L19 9l-5.5 1.5L12 16l-1.5-5.5L5 9l5.5-1.5z" /><path d="m19 16 .7 2.3L22 19l-2.3.7L19 22l-.7-2.3L16 19l2.3-.7z" /></>,
  pulse: <path d="M3 12h4l2-6 4 12 2-6h6" />,
  settings: <><circle cx="12" cy="12" r="3" /><path d="M19 15l1 2-3 3-2-1a8 8 0 0 1-2 1v2H9v-2l-2-1-2 1-3-3 1-2-1-2H0V9h2l1-2-1-2 3-3 2 1 2-1V0h4v2l2 1 2-1 3 3-1 2 1 2h2v4h-2z" /></>,
  plus: <path d="M12 5v14M5 12h14" />, arrow: <path d="M5 12h14M14 7l5 5-5 5" />,
  shield: <path d="M12 3 20 6v5c0 5-3.4 8.5-8 10-4.6-1.5-8-5-8-10V6z" />, close: <path d="m7 7 10 10M17 7 7 17" />,
  chart: <><path d="M4 19V5M4 19h16"/><path d="m7 15 4-4 3 2 5-7"/></>, star: <path d="m12 3 2.7 5.5 6.1.9-4.4 4.3 1 6.1-5.4-2.9-5.4 2.9 1-6.1-4.4-4.3 6.1-.9z"/>,
  trade: <><path d="M5 7h14l-3-3M19 17H5l3 3"/></>, portfolio: <><path d="M4 8h16v11H4zM8 8V5h8v3"/></>, layers: <><path d="m12 3 9 5-9 5-9-5z"/><path d="m3 12 9 5 9-5M3 16l9 5 9-5"/></>,
}
export function Icon({ name, size = 20 }: IconProps) {
  return <svg className="icon" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[name]}</svg>
}
