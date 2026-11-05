import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { getExtractorAgreement, type AgreementRate, type ExtractorAgreement } from '../api/client'
import { Card, ErrorState, LoadingState } from '../components'

// Quality › Requirement extraction (v5 L2, ADR 0025): the text extractor measured against the
// experience and education France Travail states in its own fields, plus ROME coverage.

const pct = (v: number | null | undefined) => (v == null ? '–' : `${Math.round(v * 100)} %`)
const muted = { margin: 0, color: 'var(--color-muted)' }

function Rate({ label, r }: { label: string; r: AgreementRate }) {
  const { t } = useTranslation()
  return (
    <tr>
      <th scope="row" style={{ textAlign: 'left', fontWeight: 'normal', paddingRight: 'var(--space-3)' }}>{label}</th>
      <td>{r.n ? t('quality.agreement.rate', { value: pct(r.value), k: r.k, n: r.n, lo: pct(r.ci95?.[0]), hi: pct(r.ci95?.[1]) })
        : t('quality.agreement.noData')}</td>
    </tr>
  )
}

export function AgreementReport({ a }: { a: ExtractorAgreement }) {
  const { t } = useTranslation()
  if (a.status !== 'measured') return <p style={{ ...muted, fontSize: 'var(--font-size-sm)' }}>{t('quality.agreement.noOffers')}</p>
  return (
    <div style={{ fontSize: 'var(--font-size-sm)', display: 'flex', flexDirection: 'column', gap: 'var(--space-2)' }}>
      <p style={{ margin: 0 }}>{t('quality.agreement.rome', {
        k: a.rome_coverage.k, n: a.rome_coverage.n, value: pct(a.rome_coverage.value),
      })}</p>
      <table aria-label={t('quality.agreement.title')} style={{ borderCollapse: 'collapse' }}>
        <tbody>
          <tr><th colSpan={2} scope="colgroup" style={{ textAlign: 'left' }}>{t('quality.agreement.experience')}</th></tr>
          <Rate label={t('quality.agreement.precision')} r={a.experience.precision} />
          <Rate label={t('quality.agreement.recall')} r={a.experience.recall} />
          <tr><th colSpan={2} scope="colgroup" style={{ textAlign: 'left' }}>{t('quality.agreement.education')}</th></tr>
          <Rate label={t('quality.agreement.precision')} r={a.education.precision} />
          <Rate label={t('quality.agreement.recall')} r={a.education.recall} />
        </tbody>
      </table>
      {a.experience.beginner_conflicts > 0 && (
        <p style={{ margin: 0 }}>{t('quality.agreement.beginnerConflicts', { count: a.experience.beginner_conflicts })}</p>
      )}
      <p style={muted}>{t('quality.agreement.rules', { offers: a.offers, years: a.years_tolerance })}</p>
    </div>
  )
}

export default function ExtractorAgreementCard() {
  const { t } = useTranslation()
  const q = useQuery({ queryKey: ['extractor-agreement'], queryFn: getExtractorAgreement })
  return (
    <Card title={t('quality.agreement.title')} subtitle={t('quality.agreement.subtitle')} style={{ marginBottom: 'var(--space-4)' }}>
      {q.isLoading && <LoadingState />}
      {q.isError && <ErrorState message={t('quality.agreement.error')} onRetry={() => void q.refetch()} />}
      {q.data && <AgreementReport a={q.data} />}
    </Card>
  )
}
