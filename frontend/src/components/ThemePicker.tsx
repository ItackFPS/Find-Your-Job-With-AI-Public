import { useId } from 'react'
import { useTranslation } from 'react-i18next'
import { useTheme } from '../theme/useTheme'
import type { ThemePreference } from '../theme/theme'

const OPTIONS: ThemePreference[] = ['system', 'light', 'dark']

/** Light / dark / follow-the-system choice, stored per browser. */
export function ThemePicker() {
  const { t } = useTranslation()
  const [preference, setPreference] = useTheme()
  const id = useId()
  return (
    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 'var(--space-2)', fontSize: 'var(--font-size-xs)', color: 'var(--color-muted)' }}>
      <label htmlFor={id}>{t('app.theme.label')}</label>
      <select
        id={id}
        value={preference}
        onChange={e => setPreference(e.target.value as ThemePreference)}
        style={{
          fontSize: 'var(--font-size-xs)',
          border: '1px solid var(--color-border-strong)',
          borderRadius: 'var(--radius-sm)',
          padding: '0.15rem 0.3rem',
        }}
      >
        {OPTIONS.map(o => <option key={o} value={o}>{t(`app.theme.${o}`)}</option>)}
      </select>
    </div>
  )
}
