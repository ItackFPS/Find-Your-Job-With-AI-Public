import { useMemo, useState } from 'react'
import { Trans, useTranslation } from 'react-i18next'
import {
  AppLogo, Button, Card, Chip, EmptyState, ErrorState, LoadingState, ProgressBar, StatTile, StatusBadge,
  Stepper, Switch, useToast,
} from '../components'
import { contrastRatio, parseColour } from '../theme/contrast'
import type { ResolvedTheme } from '../theme/theme'

// Developer-facing reference for the design system (F10.1): every token and primitive,
// rendered in the light and the dark theme side by side. Not linked from the sidebar;
// open /design directly.

const COLOUR_TOKENS = [
  'bg', 'surface', 'surface-elevated', 'border', 'border-strong', 'text', 'muted', 'accent', 'accent-dim',
  'on-accent', 'success', 'warning', 'danger', 'info', 'focus', 'link', 'tier-strong',
]
const TEXT_TOKENS = ['text', 'muted', 'accent', 'link', 'success', 'warning', 'danger', 'info', 'tier-strong']
const TYPE_SCALE = ['xs', 'sm', 'base', 'md', 'lg', 'xl', '2xl']
const SPACING = ['1', '2', '3', '4', '5', '6', '8', '10', '12']
const STEP_KEYS = ['about', 'experience', 'wishes', 'where'] as const

const sectionTitle = {
  fontSize: 'var(--font-size-xs)', fontWeight: 'var(--font-weight-semibold)', color: 'var(--color-muted)',
  textTransform: 'uppercase' as const, letterSpacing: '0.05em', margin: 'var(--space-6) 0 var(--space-2)',
}

function ContrastTable({ panel }: { panel: HTMLElement | null }) {
  const { t } = useTranslation()
  const rows = useMemo(() => {
    if (!panel) return []
    const style = getComputedStyle(panel)
    const read = (name: string) => style.getPropertyValue(`--color-${name}`).trim()
    try {
      const bg = parseColour(read('surface'))
      return TEXT_TOKENS.map(token => ({ token, ratio: contrastRatio(parseColour(read(token)), bg) }))
    } catch {
      return []
    }
  }, [panel])
  if (rows.length === 0) return null
  return (
    <table style={{ fontSize: 'var(--font-size-sm)', borderCollapse: 'collapse', background: 'var(--color-surface)' }}>
      <tbody>
        {rows.map(r => (
          <tr key={r.token}>
            <td style={{ padding: '0.15rem 0.75rem', color: `var(--color-${r.token})` }}>{r.token}</td>
            <td style={{ padding: '0.15rem 0.75rem', fontVariantNumeric: 'tabular-nums' }}>
              {t('design.ratio', { ratio: r.ratio.toFixed(2), verdict: r.ratio >= 4.5 ? t('design.aa') : t('design.belowAa') })}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function ThemePanel({ theme }: { theme: ResolvedTheme }) {
  // A callback ref: the contrast table reads this panel's computed tokens once it exists.
  const [panel, setPanel] = useState<HTMLDivElement | null>(null)
  const { t } = useTranslation()
  const STEPS = STEP_KEYS.map(key => ({ key, label: t(`design.steps.${key}`) }))
  const [checked, setChecked] = useState(true)
  const [step, setStep] = useState(1)
  const toast = useToast()

  return (
    <div ref={setPanel} data-theme={theme} style={{ flex: '1 1 420px', minWidth: 0, padding: 'var(--space-6)', borderRadius: 'var(--radius-lg)', border: '1px solid var(--color-border)' }}>
      <h2 style={{ marginTop: 0, fontSize: 'var(--font-size-xl)' }}>{theme === 'light' ? t('design.light') : t('design.dark')}</h2>

      <div style={sectionTitle}>{t('design.sections.colour')}</div>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(130px, 1fr))', gap: 'var(--space-2)' }}>
        {COLOUR_TOKENS.map(name => (
          <div key={name} style={{ fontSize: 'var(--font-size-xs)' }}>
            <div style={{ height: '36px', borderRadius: 'var(--radius-sm)', background: `var(--color-${name})`, border: '1px solid var(--color-border)' }} />
            <code>--color-{name}</code>
          </div>
        ))}
      </div>

      <div style={sectionTitle}>{t('design.sections.contrast')}</div>
      <ContrastTable panel={panel} />

      <div style={sectionTitle}>{t('design.sections.type')}</div>
      {TYPE_SCALE.map(size => (
        <div key={size} style={{ fontSize: `var(--font-size-${size})`, lineHeight: 'var(--line-height-tight)', marginBottom: 'var(--space-1)' }}>
          <code>{size}</code> · {t('design.sample')}
        </div>
      ))}

      <div style={sectionTitle}>{t('design.sections.spacing')}</div>
      {SPACING.map(n => (
        <div key={n} style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-2)', fontSize: 'var(--font-size-xs)' }}>
          <code style={{ width: '5rem' }}>--space-{n}</code>
          <span style={{ height: '8px', width: `var(--space-${n})`, background: 'var(--color-accent)', borderRadius: 2 }} />
        </div>
      ))}

      <div style={sectionTitle}>{t('design.sections.radius')}</div>
      <div style={{ display: 'flex', gap: 'var(--space-3)', flexWrap: 'wrap' }}>
        {['sm', 'md', 'lg'].map(r => (
          <div key={r} style={{ width: 72, height: 48, background: 'var(--color-surface)', borderRadius: `var(--radius-${r})`, boxShadow: `var(--shadow-${r})`, border: '1px solid var(--color-border)', fontSize: 'var(--font-size-xs)', display: 'flex', alignItems: 'center', justifyContent: 'center' }} translate="no">
            {r}
          </div>
        ))}
      </div>

      <div style={sectionTitle}>{t('design.sections.buttons')}</div>
      <div style={{ display: 'flex', gap: 'var(--space-2)', flexWrap: 'wrap' }}>
        <Button variant="primary">{t('design.buttons.primary')}</Button>
        <Button variant="secondary">{t('design.buttons.secondary')}</Button>
        <Button variant="ghost">{t('design.buttons.ghost')}</Button>
        <Button variant="danger">{t('design.buttons.danger')}</Button>
        <Button variant="primary" disabled>{t('design.buttons.disabled')}</Button>
      </div>

      <div style={sectionTitle}>{t('design.sections.chips')}</div>
      <div style={{ display: 'flex', gap: 'var(--space-2)', flexWrap: 'wrap', alignItems: 'center' }}>
        {(['default', 'accent', 'success', 'warning', 'error', 'info'] as const).map(tone => <Chip key={tone} tone={tone}><span translate="no">{tone}</span></Chip>)}
      </div>
      <div style={{ display: 'flex', gap: 'var(--space-2)', flexWrap: 'wrap', marginTop: 'var(--space-2)' }}>
        {['completed', 'running', 'failed', 'paused', 'skipped', 'unknown'].map(s => <StatusBadge key={s} status={s} />)}
      </div>

      <div style={sectionTitle}>{t('design.sections.controls')}</div>
      <Switch checked={checked} onChange={setChecked} label={t('design.switch')} description={t('design.switchDescription')} />
      <div style={{ marginTop: 'var(--space-3)' }}>
        <ProgressBar value={62} label={t('design.progress')} rightLabel="62%" />
      </div>
      <div style={{ marginTop: 'var(--space-3)' }}>
        <ProgressBar indeterminate label={t('design.indeterminate')} tone="success" />
      </div>

      <div style={sectionTitle}>{t('design.sections.stepper')}</div>
      <Stepper steps={STEPS} current={step} label={t('design.steps.label')} />
      <div style={{ display: 'flex', gap: 'var(--space-2)', marginTop: 'var(--space-2)' }}>
        <Button variant="ghost" onClick={() => setStep(s => Math.max(0, s - 1))}>{t('design.back')}</Button>
        <Button variant="ghost" onClick={() => setStep(s => Math.min(STEPS.length - 1, s + 1))}>{t('design.next')}</Button>
      </div>

      <div style={sectionTitle}>{t('design.sections.cards')}</div>
      <Card title={t('design.card.title')} subtitle={t('design.card.subtitle')} actions={<Chip tone="accent">{t('design.card.action')}</Chip>}>
        <div style={{ display: 'flex', gap: 'var(--space-2)', flexWrap: 'wrap' }}>
          <StatTile label={t('design.card.jobs')} value={1234} />
          <StatTile label={t('design.card.matches')} value={42} tone="success" />
          <StatTile label={t('design.card.loading')} value={null} loading />
        </div>
      </Card>

      <div style={sectionTitle}>{t('design.sections.states')}</div>
      <Card compact>
        <EmptyState message={t('design.empty')} action={<Button variant="primary">{t('design.emptyAction')}</Button>} />
      </Card>
      <div style={{ marginTop: 'var(--space-2)' }}>
        <LoadingState />
      </div>
      <ErrorState message={t('design.error')} details={t('design.errorDetails')} onRetry={() => toast.show({ message: t('design.retrying') })} />

      <div style={sectionTitle}>{t('design.sections.toasts')}</div>
      <div style={{ display: 'flex', gap: 'var(--space-2)', flexWrap: 'wrap' }}>
        {(['info', 'success', 'warning', 'danger'] as const).map(tone => (
          <Button key={tone} translate="no" variant="secondary" onClick={() => toast.show({ tone, message: t('design.toast', { tone }) })}>
            {tone}
          </Button>
        ))}
      </div>
    </div>
  )
}

export default function DesignSystem() {
  const { t } = useTranslation()
  return (
    <div>
      <h1 style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-3)', fontSize: 'var(--font-size-xl)', marginTop: 0 }}>
        <AppLogo size={32} /> {t('design.title')}
      </h1>
      <p style={{ color: 'var(--color-muted)', marginTop: 0 }}>
        <Trans
          i18nKey="design.intro"
          components={{ tokens: <code>src/styles/tokens.css</code>, test: <code>src/theme/__tests__/contrast.test.ts</code> }}
        />
      </p>
      <div style={{ display: 'flex', gap: 'var(--space-4)', flexWrap: 'wrap', alignItems: 'flex-start' }}>
        <ThemePanel theme="light" />
        <ThemePanel theme="dark" />
      </div>
    </div>
  )
}
