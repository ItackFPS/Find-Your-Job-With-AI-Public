import { useMemo, useState } from 'react'
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
const STEPS = [
  { key: 'about', label: 'About me' },
  { key: 'experience', label: 'Experience' },
  { key: 'wishes', label: 'What I want' },
  { key: 'where', label: 'Where' },
]

const sectionTitle = {
  fontSize: 'var(--font-size-xs)', fontWeight: 'var(--font-weight-semibold)', color: 'var(--color-muted)',
  textTransform: 'uppercase' as const, letterSpacing: '0.05em', margin: 'var(--space-6) 0 var(--space-2)',
}

function ContrastTable({ panel }: { panel: HTMLElement | null }) {
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
              {r.ratio.toFixed(2)}:1 {r.ratio >= 4.5 ? 'AA' : 'below AA'}
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
  const [checked, setChecked] = useState(true)
  const [step, setStep] = useState(1)
  const toast = useToast()

  return (
    <div ref={setPanel} data-theme={theme} style={{ flex: '1 1 420px', minWidth: 0, padding: 'var(--space-6)', borderRadius: 'var(--radius-lg)', border: '1px solid var(--color-border)' }}>
      <h2 style={{ marginTop: 0, fontSize: 'var(--font-size-xl)' }}>{theme === 'light' ? 'Light theme' : 'Dark theme'}</h2>

      <div style={sectionTitle}>Colour</div>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(130px, 1fr))', gap: 'var(--space-2)' }}>
        {COLOUR_TOKENS.map(name => (
          <div key={name} style={{ fontSize: 'var(--font-size-xs)' }}>
            <div style={{ height: '36px', borderRadius: 'var(--radius-sm)', background: `var(--color-${name})`, border: '1px solid var(--color-border)' }} />
            <code>--color-{name}</code>
          </div>
        ))}
      </div>

      <div style={sectionTitle}>Text contrast on surface</div>
      <ContrastTable panel={panel} />

      <div style={sectionTitle}>Type scale</div>
      {TYPE_SCALE.map(size => (
        <div key={size} style={{ fontSize: `var(--font-size-${size})`, lineHeight: 'var(--line-height-tight)', marginBottom: 'var(--space-1)' }}>
          {size} · The quick brown fox
        </div>
      ))}

      <div style={sectionTitle}>Spacing</div>
      {SPACING.map(n => (
        <div key={n} style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-2)', fontSize: 'var(--font-size-xs)' }}>
          <code style={{ width: '5rem' }}>--space-{n}</code>
          <span style={{ height: '8px', width: `var(--space-${n})`, background: 'var(--color-accent)', borderRadius: 2 }} />
        </div>
      ))}

      <div style={sectionTitle}>Radius and shadow</div>
      <div style={{ display: 'flex', gap: 'var(--space-3)', flexWrap: 'wrap' }}>
        {['sm', 'md', 'lg'].map(r => (
          <div key={r} style={{ width: 72, height: 48, background: 'var(--color-surface)', borderRadius: `var(--radius-${r})`, boxShadow: `var(--shadow-${r})`, border: '1px solid var(--color-border)', fontSize: 'var(--font-size-xs)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
            {r}
          </div>
        ))}
      </div>

      <div style={sectionTitle}>Buttons</div>
      <div style={{ display: 'flex', gap: 'var(--space-2)', flexWrap: 'wrap' }}>
        <Button variant="primary">Primary</Button>
        <Button variant="secondary">Secondary</Button>
        <Button variant="ghost">Ghost</Button>
        <Button variant="danger">Danger</Button>
        <Button variant="primary" disabled>Disabled</Button>
      </div>

      <div style={sectionTitle}>Chips and status</div>
      <div style={{ display: 'flex', gap: 'var(--space-2)', flexWrap: 'wrap', alignItems: 'center' }}>
        {(['default', 'accent', 'success', 'warning', 'error', 'info'] as const).map(tone => <Chip key={tone} tone={tone}>{tone}</Chip>)}
      </div>
      <div style={{ display: 'flex', gap: 'var(--space-2)', flexWrap: 'wrap', marginTop: 'var(--space-2)' }}>
        {['completed', 'running', 'failed', 'paused', 'skipped', 'unknown'].map(s => <StatusBadge key={s} status={s} />)}
      </div>

      <div style={sectionTitle}>Controls</div>
      <Switch checked={checked} onChange={setChecked} label="A switch" description="With a description" />
      <div style={{ marginTop: 'var(--space-3)' }}>
        <ProgressBar value={62} label="Progress" rightLabel="62%" />
      </div>
      <div style={{ marginTop: 'var(--space-3)' }}>
        <ProgressBar indeterminate label="Indeterminate" tone="success" />
      </div>

      <div style={sectionTitle}>Stepper</div>
      <Stepper steps={STEPS} current={step} label="Example steps" />
      <div style={{ display: 'flex', gap: 'var(--space-2)', marginTop: 'var(--space-2)' }}>
        <Button variant="ghost" onClick={() => setStep(s => Math.max(0, s - 1))}>Back</Button>
        <Button variant="ghost" onClick={() => setStep(s => Math.min(STEPS.length - 1, s + 1))}>Next</Button>
      </div>

      <div style={sectionTitle}>Cards and tiles</div>
      <Card title="Card title" subtitle="Subtitle" actions={<Chip tone="accent">Action</Chip>}>
        <div style={{ display: 'flex', gap: 'var(--space-2)', flexWrap: 'wrap' }}>
          <StatTile label="Jobs" value={1234} />
          <StatTile label="Matches" value={42} tone="success" />
          <StatTile label="Loading" value={null} loading />
        </div>
      </Card>

      <div style={sectionTitle}>Empty, loading and error states</div>
      <Card compact>
        <EmptyState message="Nothing here yet." action={<Button variant="primary">Do the first thing</Button>} />
      </Card>
      <div style={{ marginTop: 'var(--space-2)' }}>
        <LoadingState message="Loading…" />
      </div>
      <ErrorState message="Could not load the jobs." details="The local server did not answer." onRetry={() => toast.show({ message: 'Retrying…' })} />

      <div style={sectionTitle}>Toasts</div>
      <div style={{ display: 'flex', gap: 'var(--space-2)', flexWrap: 'wrap' }}>
        {(['info', 'success', 'warning', 'danger'] as const).map(tone => (
          <Button key={tone} variant="secondary" onClick={() => toast.show({ tone, message: `A ${tone} message.` })}>
            {tone}
          </Button>
        ))}
      </div>
    </div>
  )
}

export default function DesignSystem() {
  return (
    <div>
      <h1 style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-3)', fontSize: 'var(--font-size-xl)', marginTop: 0 }}>
        <AppLogo size={32} /> Design system
      </h1>
      <p style={{ color: 'var(--color-muted)', marginTop: 0 }}>
        Tokens live in <code>src/styles/tokens.css</code>. Contrast is checked by <code>src/theme/__tests__/contrast.test.ts</code>.
      </p>
      <div style={{ display: 'flex', gap: 'var(--space-4)', flexWrap: 'wrap', alignItems: 'flex-start' }}>
        <ThemePanel theme="light" />
        <ThemePanel theme="dark" />
      </div>
    </div>
  )
}
