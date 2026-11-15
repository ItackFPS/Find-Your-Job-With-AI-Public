import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import {
  getOnboarding, getProfile, startRun, updateOnboarding, updateProfile, type CareerLevel, type Profile,
} from '../api/client'
import { Button, Card, ErrorState, LanguagePicker, LoadingState, Stepper } from '../components'
import { BackgroundCard, ModelDownloadCard, ONBOARDING_QUERY_KEY, SourcesCard } from '../components/settings'
import { CheckboxGroup, DomainPicker, HomePicker } from './Profile'

const STEPS = ['welcome', 'profile', 'sources', 'model', 'background', 'done'] as const
const LEVELS: CareerLevel[] = ['internship', 'alternance', 'graduate', 'junior', 'mid', 'senior', 'lead', 'manager']

const stack = { display: 'flex', flexDirection: 'column', gap: 'var(--space-4)' } as const
// Before the first save, the profile on offer is the shipped example: never the user's search.
const blank = (p: Profile): Profile => ({
  ...p,
  target_domains: [],
  target_skills: [],
  target_skills_esco: [],
  geography: { ...p.geography, home: null },
  targets: { levels: [], stretch: false, years_override: null },
})

const fieldLabel = { display: 'block', fontWeight: 600, marginBottom: 'var(--space-2)' }

function ProfileStep({ draft, onChange }: { draft: Profile; onChange: (p: Profile) => void }) {
  const { t } = useTranslation()
  const targets = draft.targets ?? { levels: [], stretch: false, years_override: null }
  return (
    <Card title={t('onboarding.profile.title')} subtitle={t('onboarding.profile.subtitle')}>
      <div style={{ ...stack, marginTop: 'var(--space-3)' }}>
        <div>
          <span style={fieldLabel}>{t('profile.fields.targetDomains')}</span>
          <DomainPicker
            values={draft.target_domains}
            onChange={v => onChange({ ...draft, target_domains: v })}
            occupations={draft.target_occupations ?? []}
            onOccupationsChange={v => onChange({ ...draft, target_occupations: v })}
          />
        </div>
        <div>
          <span style={fieldLabel}>{t('profile.fields.home')}</span>
          <HomePicker value={draft.geography.home ?? null}
            onChange={v => onChange({ ...draft, geography: { ...draft.geography, home: v } })} />
        </div>
        <div>
          <span style={fieldLabel}>{t('profile.fields.targetLevels')}</span>
          <CheckboxGroup options={LEVELS} values={targets.levels}
            onChange={v => onChange({ ...draft, targets: { ...targets, levels: v as CareerLevel[] } })}
            labelFor={l => t(`profile.review.levels.${l}`)} />
        </div>
        <p style={{ margin: 0, color: 'var(--color-muted)', fontSize: 'var(--font-size-sm)' }}>{t('onboarding.profile.later')}</p>
      </div>
    </Card>
  )
}

/**
 * First-run setup: from install to the first search with no file edit and no command. Each step
 * reuses the Settings card that keeps working afterwards. The step is stored by the backend, so
 * closing the window resumes here. Nothing is searched before the profile is saved.
 */
export default function Onboarding() {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const state = useQuery({ queryKey: ONBOARDING_QUERY_KEY, queryFn: getOnboarding })
  const remote = useQuery({ queryKey: ['profile'], queryFn: getProfile })
  const [step, setStep] = useState<number | null>(null)
  const [draft, setDraft] = useState<Profile | null>(null)

  const current = step ?? state.data?.step ?? 0
  const name = STEPS[Math.min(current, STEPS.length - 1)]

  const loaded = state.data !== undefined
  const profileSaved = state.data?.profile_saved === true
  useEffect(() => {
    if (remote.data && loaded && !draft) setDraft(profileSaved ? remote.data : blank(remote.data))
  }, [remote.data, loaded, profileSaved, draft])

  // Marks the setup as begun at once: its own profile save must not read as "already set up".
  const persist = useMutation({
    mutationFn: updateOnboarding,
    onSuccess: saved => queryClient.setQueryData(ONBOARDING_QUERY_KEY, saved),
  })
  const { mutate: persistStep } = persist
  const startedStep = state.data?.started ? null : state.data?.step
  useEffect(() => {
    if (startedStep !== undefined && startedStep !== null) persistStep({ step: startedStep })
  }, [startedStep, persistStep])

  const go = (next: number) => {
    setStep(next)
    persist.mutate({ step: next })
  }
  const saveProfile = useMutation({
    mutationFn: (p: Profile) => updateProfile(p),
    onSuccess: saved => {
      setDraft(saved)
      queryClient.setQueryData(['profile'], saved)
      void queryClient.invalidateQueries({ queryKey: ['profile'] })
      go(current + 1)
    },
  })
  const finish = useMutation({
    mutationFn: (run: boolean) => updateOnboarding({ completed: true }).then(saved => {
      queryClient.setQueryData(ONBOARDING_QUERY_KEY, saved)
      return run ? startRun().then(() => undefined) : undefined
    }),
    onSuccess: () => navigate('/'),
  })

  if (state.isLoading || remote.isLoading || (state.data && remote.data && !draft)) return <LoadingState />
  if (state.isError || remote.isError || !draft) {
    return <ErrorState message={t('onboarding.loadFailed')} onRetry={() => { void state.refetch(); void remote.refetch() }} />
  }

  const profileReady = draft.target_domains.length > 0 && Boolean(draft.geography.home)

  return (
    <div style={{ ...stack, maxWidth: '50rem', margin: '0 auto' }}>
      <h1 style={{ margin: 0 }}>{t('onboarding.title')}</h1>
      <Stepper steps={STEPS.map(key => ({ key, label: t(`onboarding.steps.${key}`) }))} current={current} label={t('onboarding.progress')} />

      {name === 'welcome' && (
        <Card title={t('onboarding.welcome.title')}>
          <div style={{ ...stack, marginTop: 'var(--space-3)' }}>
            <LanguagePicker />
            <p style={{ margin: 0 }}>{t('onboarding.welcome.intro')}</p>
            <p style={{ margin: 0, color: 'var(--color-muted)' }}>{t('onboarding.welcome.scope')}</p>
          </div>
        </Card>
      )}
      {name === 'profile' && <ProfileStep draft={draft} onChange={setDraft} />}
      {name === 'sources' && <SourcesCard />}
      {name === 'model' && <ModelDownloadCard />}
      {name === 'background' && <BackgroundCard />}
      {name === 'done' && (
        <Card title={t('onboarding.done.title')}>
          <p style={{ marginBottom: 0 }}>{t('onboarding.done.body')}</p>
        </Card>
      )}

      {saveProfile.isError && <p role="alert" style={{ color: 'var(--color-error)', margin: 0 }}>{t('onboarding.profile.saveFailed')}</p>}
      {finish.isError && <p role="alert" style={{ color: 'var(--color-error)', margin: 0 }}>{t('onboarding.done.failed')}</p>}

      <div style={{ display: 'flex', justifyContent: 'space-between', gap: 'var(--space-3)', flexWrap: 'wrap' }}>
        <div style={{ display: 'flex', gap: 'var(--space-2)' }}>
          {current > 0 && <Button variant="secondary" onClick={() => go(current - 1)}>{t('onboarding.back')}</Button>}
          {name !== 'done' && <Button variant="ghost" onClick={() => finish.mutate(false)}>{t('onboarding.skip')}</Button>}
        </div>
        <div style={{ display: 'flex', gap: 'var(--space-2)', alignItems: 'center' }}>
          {name === 'profile' && !profileReady && (
            <span style={{ fontSize: 'var(--font-size-sm)', color: 'var(--color-muted)' }}>{t('onboarding.profile.needed')}</span>
          )}
          {name === 'profile' && (
            <Button disabled={!profileReady || saveProfile.isPending} onClick={() => saveProfile.mutate(draft)}>{t('onboarding.next')}</Button>
          )}
          {name !== 'profile' && name !== 'done' && <Button onClick={() => go(current + 1)}>{t('onboarding.next')}</Button>}
          {name === 'done' && (
            <>
              <Button variant="secondary" disabled={finish.isPending} onClick={() => finish.mutate(false)}>{t('onboarding.done.later')}</Button>
              <Button disabled={finish.isPending} onClick={() => finish.mutate(true)}>{t('onboarding.done.search')}</Button>
            </>
          )}
        </div>
      </div>
    </div>
  )
}
