import { useId } from 'react'
import { useTranslation } from 'react-i18next'
import { availableLanguages, setLanguage, type Language } from '../i18n'

/** UI language choice, stored per browser. The pseudo-locale is listed in development only. */
export function LanguagePicker() {
  const { t, i18n } = useTranslation()
  const id = useId()
  return (
    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 'var(--space-2)', fontSize: 'var(--font-size-xs)', color: 'var(--color-muted)' }}>
      <label htmlFor={id}>{t('app.language.label')}</label>
      <select
        id={id}
        value={i18n.language}
        onChange={e => setLanguage(e.target.value as Language)}
        style={{
          fontSize: 'var(--font-size-xs)',
          border: '1px solid var(--color-border-strong)',
          borderRadius: 'var(--radius-sm)',
          padding: '0.15rem 0.3rem',
        }}
      >
        {availableLanguages().map(lng => (
          <option key={lng} value={lng} lang={lng === 'en-XA' ? 'en' : lng}>{t(`app.language.${lng}`)}</option>
        ))}
      </select>
    </div>
  )
}
