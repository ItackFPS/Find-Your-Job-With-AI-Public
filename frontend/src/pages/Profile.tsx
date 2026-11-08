import { useState, useEffect } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { getProfile, updateProfile, confirmProfileReview, type Profile, type RemotePolicy, type CareerLevel } from '../api/client'
import { useTranslation } from 'react-i18next'
import { Card, LoadingState, Switch } from '../components'
import { ProfileReviewCard } from './ProfileReviewCard'
import { EducationEditor } from './EducationEditor'
import { ExperienceEditor } from './ExperienceEditor'
import { AutocompleteTagList, TagList } from './profile/TagInputs'
import type { SkillsState } from './profile/TagInputs'
import { CheckboxGroup, CountBadge, SegmentedControl } from './profile/Controls'
import { DomainPicker } from './profile/DomainPicker'
import { HomePicker, LocationPreferencesEditor } from './profile/LocationEditors'
import { fieldGroup, inputStyle, label, sectionDivider } from './profile/shared'

const REMOTE_POLICY_KEYS: (keyof RemotePolicy)[] = ['remote_france', 'remote_eu', 'remote_worldwide', 'hybrid']

const EDUCATION_LEVELS = ['bac', 'bac+1', 'bac+2', 'bac+3', 'bac+4', 'bac+5', 'bac+8']

export default function ProfilePage() {
  const { t } = useTranslation()
  const optionLabel = (opt: string) => t(`profile.options.${opt}`)
  const queryClient = useQueryClient()

  const { data: serverProfile, isLoading, error } = useQuery({
    queryKey: ['profile'],
    queryFn: getProfile,
  })

  const [profile, setProfile] = useState<Profile | null>(null)
  const [saved, setSaved] = useState(false)

  useEffect(() => {
    if (serverProfile && !profile) setProfile(serverProfile)
  }, [serverProfile, profile])

  const { mutate: save, isPending, error: saveError } = useMutation({
    mutationFn: (p: Profile) => updateProfile(p),
    onSuccess: (updated) => {
      setProfile(updated)
      queryClient.invalidateQueries({ queryKey: ['profile'] })
      setSaved(true)
      setTimeout(() => setSaved(false), 2500)
    },
  })

  const { mutate: confirmReview, isPending: confirming } = useMutation({
    mutationFn: confirmProfileReview,
    onSuccess: (updated) => {
      // Keep the page's unsaved edits; only the review state comes from the server.
      setProfile(p => p ? { ...p, review: updated.review ?? null } : p)
      queryClient.invalidateQueries({ queryKey: ['profile'] })
    },
  })

  if (isLoading) return <LoadingState />
  if (error) return <div style={{ color: 'var(--color-red)', padding: '2rem' }}>{t('profile.loadError')}</div>
  if (!profile) return null

  const set = <K extends keyof Profile>(key: K, value: Profile[K]) =>
    setProfile(p => p ? { ...p, [key]: value } : p)

  const setGeo = <K extends keyof Profile['geography']>(key: K, value: Profile['geography'][K]) =>
    setProfile(p => p ? { ...p, geography: { ...p.geography, [key]: value } } : p)

  const setExp = <K extends keyof Profile['experience']>(key: K, value: Profile['experience'][K]) =>
    setProfile(p => p ? { ...p, experience: { ...p.experience, [key]: value } } : p)

  const skillsState: SkillsState = {
    skills: profile.target_skills,
    escoEntries: profile.target_skills_esco ?? [],
  }

  const setSkillsState = (s: SkillsState) =>
    setProfile(p => p ? { ...p, target_skills: s.skills, target_skills_esco: s.escoEntries } : p)

  const workModeOptions = ['remote', 'hybrid', 'on-site']
  const contractOptions = ['full-time', 'part-time', 'internship', 'freelance']
  const seniorityOptions = ['junior', 'mid', 'senior', 'lead', 'director', 'executive']
  const levelOptions = ['junior', 'mid', 'senior', 'lead']
  // Profile v2: target career levels (F5.2) and education levels (F5.3) replace the v1 fields.
  const targets = (profile.profile_version ?? 1) >= 2 ? profile.targets ?? null : null
  const careerLevels: CareerLevel[] = ['internship', 'alternance', 'graduate', 'junior', 'mid', 'senior', 'lead', 'manager']

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '1.5rem', flexWrap: 'wrap', gap: '0.75rem' }}>
        <h1 style={{ margin: 0, fontSize: '1.4rem', fontWeight: 700 }}>{t('profile.title')}</h1>
        <div style={{ display: 'flex', gap: '0.5rem', alignItems: 'center' }}>
          {saved && <span style={{ color: 'var(--color-green)', fontSize: '0.85rem' }}>{t('profile.saved')}</span>}
          {saveError && <span style={{ color: 'var(--color-red)', fontSize: '0.85rem' }}>{t('profile.saveFailed')}</span>}
          <button
            onClick={() => save(profile)}
            disabled={isPending}
            style={{
              background: isPending ? 'var(--color-border)' : 'var(--color-accent)',
              color: 'var(--color-on-accent)',
              border: 'none',
              borderRadius: '7px',
              padding: '0.45rem 1.1rem',
              fontSize: '0.88rem',
              fontWeight: 600,
              cursor: isPending ? 'not-allowed' : 'pointer',
            }}
          >
            {isPending ? t('profile.saving') : t('profile.save')}
          </button>
        </div>
      </div>

      <ProfileReviewCard
        profile={profile}
        saving={confirming}
        onConfirm={() => { if (profile.review) confirmReview(profile.review) }}
      />

      <Card style={{ marginBottom: '1rem' }} title={t('profile.sections.targets')} subtitle={t('profile.sections.targetsSubtitle')}>
        <div style={{ marginTop: 'var(--space-4)' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: '0.5rem', marginBottom: '0.5rem' }}>
            <span style={{ ...label, marginBottom: 0 }}>{t('profile.fields.targetDomains')}<CountBadge count={profile.target_domains.length} /></span>
            <Switch
              checked={profile.focus_domain}
              onChange={v => set('focus_domain', v)}
              label={profile.focus_domain ? t('profile.fields.focusDomain') : t('profile.fields.focusSkills')}
            />
          </div>
          <DomainPicker values={profile.target_domains} onChange={v => set('target_domains', v)} />
        </div>

        <hr style={sectionDivider} />

        <div>
          <span style={label}>{t('profile.fields.targetSkills')}<CountBadge count={profile.target_skills.length} /></span>
          <AutocompleteTagList state={skillsState} onChange={setSkillsState} />
        </div>

        <hr style={sectionDivider} />

        <div>
          <span style={label}>{t('profile.fields.languages')}<CountBadge count={profile.languages.length} /></span>
          <TagList values={profile.languages} onChange={v => set('languages', v)} tone="language" placeholder={t('profile.fields.languagesPlaceholder')} />
        </div>
      </Card>

      <Card style={{ marginBottom: '1rem' }} title={t('profile.sections.geography')}>
        <div style={{ ...fieldGroup, marginTop: 'var(--space-4)' }}>
          <span style={label}>{t('profile.fields.countries')}</span>
          <TagList values={profile.geography.preferred_countries} onChange={v => setGeo('preferred_countries', v)} placeholder={t('profile.fields.countriesPlaceholder')} />
        </div>

        <div style={fieldGroup}>
          <span style={label}>{t('profile.fields.remoteScope')}</span>
          <div style={{ fontSize: '0.8rem', color: 'var(--color-muted)', marginBottom: '0.5rem' }}>
            {t('profile.fields.remoteScopeHint')}
          </div>
          <SegmentedControl
            value={profile.geography.allowed_regions.includes('Europe') ? 'europe' : 'same_country'}
            onChange={v => setGeo('allowed_regions', v === 'europe' ? ['Europe'] : [])}
            options={[
              { value: 'same_country', label: t('profile.fields.sameCountry') },
              { value: 'europe', label: t('profile.fields.europe') },
            ]}
          />
        </div>

        <div style={fieldGroup}>
          <span style={label}>{t('profile.fields.home')}</span>
          <HomePicker value={profile.geography.home} onChange={v => setGeo('home', v)} />
          {!profile.geography.home && (
            <span role="note" style={{ fontSize: '0.8rem', color: 'var(--color-warning)' }}>{t('profile.fields.homePrompt')}</span>
          )}
        </div>

        <div style={fieldGroup}>
          <span style={label}>{t('profile.fields.maxDistance', { km: profile.geography.max_distance_km })}</span>
          <div style={{ fontSize: '0.8rem', color: 'var(--color-muted)', marginBottom: '0.5rem' }}>
            {t('profile.fields.maxDistanceHint')}
          </div>
          <input
            type="range" min={5} max={300} step={5}
            value={profile.geography.max_distance_km}
            onChange={e => setGeo('max_distance_km', Number(e.target.value))}
            aria-label={t('profile.fields.maxDistance', { km: profile.geography.max_distance_km })}
            style={{ width: '100%' }}
          />
          <label style={{ display: 'flex', gap: '0.5rem', alignItems: 'center', fontSize: '0.87rem', marginTop: '0.5rem' }}>
            <input
              type="checkbox"
              checked={profile.geography.strict_distance}
              onChange={e => setGeo('strict_distance', e.target.checked)}
            />
            {t('profile.fields.strictDistance')}
          </label>
        </div>

        <div style={fieldGroup}>
          <span style={label}>{t('profile.fields.remoteAccepted')}</span>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: '0.9rem' }}>
            {REMOTE_POLICY_KEYS.map(key => (
              <label key={key} style={{ display: 'flex', gap: '0.4rem', alignItems: 'center', fontSize: '0.87rem' }}>
                <input
                  type="checkbox"
                  checked={profile.geography.remote_policy[key]}
                  onChange={e => setGeo('remote_policy', { ...profile.geography.remote_policy, [key]: e.target.checked })}
                />
                {t(`profile.fields.remotePolicy.${key}`)}
              </label>
            ))}
          </div>
        </div>

        <div style={fieldGroup}>
          <span style={label}>
            {t('profile.fields.locationPriorities')}<CountBadge count={profile.geography.location_preferences.length} />
          </span>
          <div style={{ fontSize: '0.8rem', color: 'var(--color-muted)', marginBottom: '0.5rem' }}>
            {t('profile.fields.locationHomeNote')} {t('profile.fields.locationHint')}
          </div>
          <LocationPreferencesEditor
            values={profile.geography.location_preferences}
            onChange={v => setGeo('location_preferences', v)}
          />
        </div>
      </Card>

      <Card style={{ marginBottom: '1rem' }} title={t('profile.sections.work')}>
        <div style={{ ...fieldGroup, marginTop: 'var(--space-4)' }}>
          <span style={label}>{t('profile.fields.workModes')}</span>
          <CheckboxGroup options={workModeOptions} values={profile.work_modes} onChange={v => set('work_modes', v)} labelFor={optionLabel} />
        </div>

        <div style={fieldGroup}>
          <span style={label}>{t('profile.fields.contractTypes')}</span>
          <CheckboxGroup options={contractOptions} values={profile.contract_types} onChange={v => set('contract_types', v)} labelFor={optionLabel} />
        </div>

        <div style={fieldGroup}>
          <span style={label}>{t('profile.fields.freshness')}</span>
          <input
            aria-label={t('profile.fields.freshness')}
            type="number"
            value={profile.freshness_days}
            min={1}
            max={365}
            onChange={e => set('freshness_days', parseInt(e.target.value) || 30)}
            style={{ ...inputStyle, width: '100px' }}
          />
        </div>
      </Card>

      <Card style={{ marginBottom: '1rem' }} title={t('profile.sections.experience')}>
        {targets ? (
          <>
            <div style={{ ...fieldGroup, marginTop: 'var(--space-4)' }}>
              <span style={label}>{t('profile.fields.targetLevels')}</span>
              <CheckboxGroup options={careerLevels} values={targets.levels}
                onChange={v => set('targets', { ...targets, levels: v as CareerLevel[] })}
                labelFor={l => t(`profile.review.levels.${l}`)} />
              <span style={{ fontSize: '0.8rem', color: 'var(--color-muted)' }}>{t('profile.fields.targetLevelsHint')}</span>
            </div>
            <div style={fieldGroup}>
              <Switch checked={targets.stretch} onChange={v => set('targets', { ...targets, stretch: v })}
                label={t('profile.fields.stretch')} description={t('profile.fields.stretchHint')} />
            </div>
            <div style={fieldGroup}>
              <span style={label}>{t('profile.experiences.title')}</span>
              <ExperienceEditor entries={profile.experiences ?? []} onChange={v => set('experiences', v)} />
            </div>
          </>
        ) : (
          <>
            <div style={{ ...fieldGroup, marginTop: 'var(--space-4)' }}>
              <span style={label}>{t('profile.fields.preferredLevels')}</span>
              <CheckboxGroup options={levelOptions} values={profile.experience.preferred_levels} onChange={v => setExp('preferred_levels', v)} labelFor={optionLabel} />
            </div>

            <div style={fieldGroup}>
              <span style={label}>{t('profile.fields.excludedSeniority')}</span>
              <CheckboxGroup options={seniorityOptions} values={profile.experience.excluded_seniority} onChange={v => setExp('excluded_seniority', v)} />
            </div>
          </>
        )}

        <div style={fieldGroup}>
          <span style={label}>{t('profile.fields.maxYears')}</span>
          <input
            type="number"
            value={profile.experience.max_years ?? ''}
            min={0}
            max={50}
            placeholder={t('profile.fields.noLimit')}
            onChange={e => setExp('max_years', e.target.value === '' ? null : parseInt(e.target.value))}
            style={{ ...inputStyle, width: '120px' }}
          />
        </div>
      </Card>

      <Card title={t('profile.sections.education')}>
        {targets ? (
          <div style={{ ...fieldGroup, marginTop: 'var(--space-4)' }}>
            <EducationEditor entries={profile.education ?? []} onChange={v => set('education', v)} />
          </div>
        ) : (
        <div style={{ ...fieldGroup, marginTop: 'var(--space-4)' }}>
          <span style={label}>{t('profile.fields.maxEducation')}</span>
          <select
            aria-label={t('profile.fields.maxEducation')}
            value={profile.max_education ?? ''}
            onChange={e => set('max_education', e.target.value === '' ? null : e.target.value)}
            style={{ ...inputStyle, width: '160px' }}
          >
            <option value="">{t('profile.fields.noLimit')}</option>
            {EDUCATION_LEVELS.map(lvl => (
              <option key={lvl} value={lvl} translate="no">{lvl}</option>
            ))}
          </select>
        </div>
        )}
      </Card>
    </div>
  )
}
