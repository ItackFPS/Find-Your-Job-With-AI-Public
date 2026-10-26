import { useTranslation } from 'react-i18next'
import { Button } from '../components'
import type { EducationEntry } from '../api/client'

// Mirrors src/models/levels.py QUALIFICATION_LEVELS (French national nomenclature = EQF level).
const QUALIFICATION_LEVELS: Record<string, EducationEntry['level']> = {
  CAP: 3, BEP: 3,
  Bac: 4, 'Bac pro': 4, 'Bac techno': 4,
  BTS: 5, DUT: 5, DEUG: 5, DEUST: 5,
  BUT: 6, Licence: 6, 'Licence pro': 6, Bachelor: 6,
  Master: 7, "Diplôme d'ingénieur": 7, MBA: 7,
  Doctorat: 8,
}
const LEVELS = [3, 4, 5, 6, 7, 8] as const
// Case-insensitive: a migrated entry keeps the profile's old text ("master", "bac+3").
const knownName = (name: string) =>
  Object.keys(QUALIFICATION_LEVELS).find(q => q.toLowerCase() === name.trim().toLowerCase())

const input = {
  background: 'var(--color-bg)', border: '1px solid var(--color-border)', borderRadius: '6px',
  color: 'var(--color-text)', padding: '0.35rem 0.55rem', fontSize: '0.87rem',
}

/** Profile v2 education (F5.3): qualifications with their French/EQF level 3–8. A named
 *  French qualification sets its level; a foreign degree takes the level the user picks. */
export function EducationEditor({ entries, onChange }: {
  entries: EducationEntry[]
  onChange: (entries: EducationEntry[]) => void
}) {
  const { t } = useTranslation()
  const update = (i: number, patch: Partial<EducationEntry>) =>
    onChange(entries.map((e, j) => (j === i ? { ...e, ...patch } : e)))
  const add = () => {
    const ids = new Set(entries.map(e => e.id))
    let n = entries.length + 1
    while (ids.has(`edu-${n}`)) n += 1
    onChange([...entries, { id: `edu-${n}`, qualification: 'Licence', level: 6, field: null, institution: null,
      year: null, in_progress: false, foreign: false }])
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '0.6rem' }}>
      {entries.length === 0 && <span style={{ color: 'var(--color-muted)', fontSize: '0.85rem' }}>{t('profile.education.empty')}</span>}
      {entries.map((e, i) => {
        const match = e.foreign ? undefined : knownName(e.qualification)
        const known = match !== undefined
        return (
          <div key={e.id} role="group" aria-label={t('profile.education.entry', { n: i + 1 })}
            style={{ display: 'flex', flexWrap: 'wrap', gap: '0.5rem', alignItems: 'center' }}>
            <select aria-label={t('profile.education.qualification')} style={input}
              value={match ?? '__other__'}
              onChange={ev => {
                const v = ev.target.value
                // Keeps the current name until the user types another, so it is never empty.
                if (v === '__other__') update(i, { foreign: true })
                else update(i, { foreign: false, qualification: v, level: QUALIFICATION_LEVELS[v] })
              }}>
              {Object.keys(QUALIFICATION_LEVELS).map(q => <option key={q} value={q} translate="no">{q}</option>)}
              <option value="__other__">{t('profile.education.foreign')}</option>
            </select>
            {!known && (
              <>
                <input aria-label={t('profile.education.foreignName')} placeholder={t('profile.education.foreignName')}
                  value={e.qualification} onChange={ev => update(i, { qualification: ev.target.value })} style={input} />
                <select aria-label={t('profile.education.level')} style={input} value={e.level ?? ''}
                  onChange={ev => update(i, { level: ev.target.value === '' ? null : Number(ev.target.value) as EducationEntry['level'] })}>
                  <option value="">{t('profile.education.pickLevel')}</option>
                  {LEVELS.map(l => <option key={l} value={l}>{t(`profile.education.levelHelp.${l}`)}</option>)}
                </select>
              </>
            )}
            {known && <span style={{ fontSize: '0.85rem', color: 'var(--color-muted)' }}>{t('profile.review.levelValue', { level: e.level })}</span>}
            <label style={{ fontSize: '0.85rem', display: 'flex', gap: '0.3rem', alignItems: 'center' }}>
              <input type="checkbox" checked={e.in_progress} onChange={ev => update(i, { in_progress: ev.target.checked })} />
              {t('profile.education.inProgress')}
            </label>
            <Button variant="ghost" onClick={() => onChange(entries.filter((_, j) => j !== i))}>{t('profile.education.remove')}</Button>
          </div>
        )
      })}
      <div><Button onClick={add}>{t('profile.education.add')}</Button></div>
      <span style={{ fontSize: '0.8rem', color: 'var(--color-muted)' }}>{t('profile.education.hint')}</span>
    </div>
  )
}
