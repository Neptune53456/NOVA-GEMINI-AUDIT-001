import { Icon } from './Icon'
interface PagePlaceholderProps { title: string; description: string; icon: string; accent?: boolean }
export function PagePlaceholder({ title, description, icon, accent = false }: PagePlaceholderProps) {
  return <section className={`placeholder-page ${accent ? 'purple' : ''}`}><div className="placeholder-icon"><Icon name={icon} size={28} /></div><p className="eyebrow">ESPACE NOVA</p><h1>{title}</h1><p>{description}</p><div className="placeholder-card"><span>Cette section sera disponible dans une prochaine phase.</span></div></section>
}
