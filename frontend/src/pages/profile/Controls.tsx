export function CountBadge({ count }: { count: number }) {
  return (
    <span style={{ fontSize: '0.72rem', fontWeight: 400, color: 'var(--color-muted)', textTransform: 'none', letterSpacing: 'normal' }}>
      {count}
    </span>
  )
}

// `labelFor` translates an option code; without it the code is shown as is, marked translate="no"
// (the excluded-seniority keywords are matched literally in job titles).
export function CheckboxGroup({ options, values, onChange, labelFor }: {
  options: string[]; values: string[]; onChange: (v: string[]) => void; labelFor?: (opt: string) => string
}) {
  const toggle = (opt: string) => {
    if (values.includes(opt)) onChange(values.filter(x => x !== opt))
    else onChange([...values, opt])
  }
  return (
    <div style={{ display: 'flex', flexWrap: 'wrap', gap: '0.55rem' }}>
      {options.map(opt => (
        <button
          key={opt}
          translate={labelFor ? undefined : 'no'}
          onClick={() => toggle(opt)}
          style={{
            background: values.includes(opt) ? 'var(--color-accent)' : 'var(--color-bg)',
            border: `1px solid ${values.includes(opt) ? 'var(--color-accent)' : 'var(--color-border)'}`,
            borderRadius: '20px',
            padding: '0.4rem 1.05rem',
            fontSize: '0.87rem',
            color: values.includes(opt) ? 'var(--color-on-accent)' : 'var(--color-muted)',
            cursor: 'pointer',
            transition: 'all 0.15s',
          }}
        >
          {labelFor ? labelFor(opt) : opt}
        </button>
      ))}
    </div>
  )
}

export function SegmentedControl<T extends string>({ options, value, onChange }: {
  options: { value: T; label: string }[]
  value: T
  onChange: (v: T) => void
}) {
  return (
    <div style={{ display: 'inline-flex', border: '1px solid var(--color-border)', borderRadius: '20px', padding: '0.2rem', gap: '0.2rem' }}>
      {options.map(opt => (
        <button
          key={opt.value}
          onClick={() => onChange(opt.value)}
          style={{
            background: value === opt.value ? 'var(--color-accent)' : 'transparent',
            border: 'none',
            borderRadius: '16px',
            padding: '0.42rem 1.05rem',
            fontSize: '0.86rem',
            color: value === opt.value ? 'var(--color-on-accent)' : 'var(--color-muted)',
            cursor: 'pointer',
            fontWeight: value === opt.value ? 600 : 400,
            transition: 'all 0.15s',
          }}
        >
          {opt.label}
        </button>
      ))}
    </div>
  )
}
