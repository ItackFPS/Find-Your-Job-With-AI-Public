import { useTranslation } from 'react-i18next'
import { Button } from '../components'
import type { ExperienceEntry } from '../api/client'

const input = {
  background: 'var(--color-bg)', border: '1px solid var(--color-border)', borderRadius: '6px',
  color: 'var(--color-text)', padding: '0.35rem 0.55rem', fontSize: '0.87rem',
}

/** Profile v2 work history (F5.1): the years of experience are computed from these entries. */
export function ExperienceEditor({ entries, onChange }: {
  entries: ExperienceEntry[]
  onChange: (entries: ExperienceEntry[]) => void
}) {
  const { t } = useTranslation()
  const update = (i: number, patch: Partial<ExperienceEntry>) =>
    onChange(entries.map((e, j) => (j === i ? { ...e, ...patch } : e)))
  const add = () => {
    const ids = new Set(entries.map(e => e.id))
    let n = entries.length + 1
    while (ids.has(`exp-${n}`)) n += 1
    const now = new Date()
    const month = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}`
    onChange([...entries, { id: `exp-${n}`, role: '', employer: null, start: month, end: null,
      skills: [], responsibilities: [], relevant: true }])
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '0.6rem' }}>
      {entries.length === 0 && <span style={{ color: 'var(--color-muted)', fontSize: '0.85rem' }}>{t('profile.experiences.empty')}</span>}
      {entries.map((e, i) => (
        <div key={e.id} role="group" aria-label={t('profile.experiences.entry', { n: i + 1 })}
          style={{ display: 'flex', flexWrap: 'wrap', gap: '0.5rem', alignItems: 'center' }}>
          <input aria-label={t('profile.experiences.role')} placeholder={t('profile.experiences.role')}
            value={e.role} onChange={ev => update(i, { role: ev.target.value })} style={input} />
          <input aria-label={t('profile.experiences.employer')} placeholder={t('profile.experiences.employer')}
            value={e.employer ?? ''} onChange={ev => update(i, { employer: ev.target.value || null })} style={input} />
          <label style={{ fontSize: '0.85rem', display: 'flex', gap: '0.3rem', alignItems: 'center' }}>
            {t('profile.experiences.start')}
            <input type="month" required value={e.start}
              onChange={ev => { if (ev.target.value) update(i, { start: ev.target.value }) }} style={input} />
          </label>
          <label style={{ fontSize: '0.85rem', display: 'flex', gap: '0.3rem', alignItems: 'center' }}>
            {t('profile.experiences.end')}
            <input type="month" value={e.end ?? ''}
              onChange={ev => update(i, { end: ev.target.value || null })} style={input} />
          </label>
          <label style={{ fontSize: '0.85rem', display: 'flex', gap: '0.3rem', alignItems: 'center' }}>
            <input type="checkbox" checked={e.relevant} onChange={ev => update(i, { relevant: ev.target.checked })} />
            {t('profile.experiences.relevant')}
          </label>
          <Button variant="ghost" onClick={() => onChange(entries.filter((_, j) => j !== i))}>{t('profile.experiences.remove')}</Button>
        </div>
      ))}
      <div><Button onClick={add}>{t('profile.experiences.add')}</Button></div>
      <span style={{ fontSize: '0.8rem', color: 'var(--color-muted)' }}>{t('profile.experiences.hint')}</span>
    </div>
  )
}
