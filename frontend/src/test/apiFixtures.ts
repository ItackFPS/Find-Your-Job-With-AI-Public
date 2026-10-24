// Deterministic API payloads for page-level tests (and for a local mock API when checking
// pages by eye). Shapes follow src/api/client.ts; every value is made up.
import type {
  ActiveStrategy, Dashboard, DecisionRecord, EvalRunSummary, FeedbackEvent, FeedbackVocabulary, FrenchDepartment,
  HelloWorkScanRun, HelloWorkStatus, Job, JobExplanation, JobStats, LabExperiment, LabMemoryItem,
  LinkedInScanRun, LinkedInStatus, PipelineRun, Profile, ReviewQueuePage, RunStats, SelfCheckResponse, SkillGraph,
  CountriesResponse,
} from '../api/client'

const NOW = '2026-10-20T09:30:00+00:00'
const EARLIER = '2026-10-20T09:12:00+00:00'

export const runStats: RunStats = {
  collected: 1240, validated: 1198, deduplicated: 1010, filtered: 640,
  hard_filter_input: 1010, hard_filter_passed: 700, requirements_gate_input: 700, requirements_gate_passed: 640,
  llm_eligibility_input: 640, llm_eligibility_passed: 640, llm_eligibility_rejected: 0,
  scored: 640, semantically_matched: 640, personalized: 640, duration_seconds: 312,
  by_source: {
    remoteok: { collected: 300, accepted: 120, errors: 0, status: 'completed', reason: null },
    jobicy: { collected: 200, accepted: 90, errors: 2, status: 'partial', reason: 'HTTP 429 on page 3' },
    'france-travail': { collected: 0, accepted: 0, errors: 0, status: 'skipped', reason: 'No credentials set' },
    eures: { collected: 740, accepted: 430, errors: 0, status: 'completed', reason: null },
  },
  by_country: { France: 800, Germany: 120, Spain: 90 },
  by_work_mode: { remote: 300, hybrid: 200, onsite: 510 },
  errors: [],
  profile_hash: 'p1', model_name: 'paraphrase-multilingual-MiniLM-L12-v2', scoring_config_hash: 's1',
  current_stage: 'completed', use_ai: false,
}

export const pipelineRun: PipelineRun = {
  id: 'run-1', started_at: EARLIER, completed_at: NOW, status: 'completed', stats: runStats, error: null,
}

export const dashboard: Dashboard = {
  latest_run: pipelineRun, total_jobs_in_db: 5321, is_running: false, current_run_id: null,
}

export const job: Job = {
  id: 'job-1', source: 'remoteok', title: 'Développeur Python', company: 'Acme', location: 'Toulon, France',
  department: '83', countries: ['France'], remote: false, work_mode: 'hybrid', employment_type: 'CDI',
  salary_min: 42000, salary_max: 52000, salary_currency: 'EUR', published_at: '2026-10-18T00:00:00+00:00',
  discovered_at: NOW, skills: ['Python', 'SQL', 'Docker'], canonical_url: 'https://example.com/job/1',
  apply_url: 'https://example.com/job/1/apply', status: 'accepted', application_status: null,
  score: {
    deterministic_score: 0.78, semantic_score: 0.7, final_score: 0.76,
    factors: { location_fit: 0.9, domain_relevance: 0.7, skills_match: 0.6 },
    run_id: 'run-1', score_100: 76, tier: 'excellent',
  },
  description: 'Nous recherchons un développeur Python.',
}

export const jobs: Job[] = [
  job,
  { ...job, id: 'job-2', source: 'linkedin', title: 'Data Engineer', company: 'Globex', department: null,
    countries: ['Germany'], remote: true, work_mode: 'remote', location: 'Berlin',
    score: { ...job.score!, score_100: 66, tier: 'strong' } },
]

export const explanation: JobExplanation = {
  job_id: 'job-1', overall_score: 76, score: job.score, filter_result: 'passed', rejection_reason: null,
  positive_reasons: [{ emoji: '✅', text: 'Python matches your skills', kind: 'positive' }],
  concerns: [{ emoji: '⚠️', text: 'Salary not stated', kind: 'concern' }],
  rejection_reasons: [], explanation_source: 'decision', decision_id: 'dec-1',
  personalization_signals: [], personalization_adjustment: 0,
}

export const decision: DecisionRecord = {
  decision_id: 'dec-1', run_id: 'run-1', job_id: 'job-1', decided_at: NOW, outcome: 'accepted',
  deciding_stage: 'scoring', strategy_version: 'sv-1', fit_score: 0.76, decision_confidence: 0.8,
  confidence_calibrated: false,
  signals: { assessment: { state: 'review', confidence: 0.55, reasons: ['skills_conflict'], unknowns: ['salary'], mode: 'shadow', calibrated: false } },
  evidence: [
    { dimension: 'skills', polarity: 'positive', state: 'satisfied', message: 'Python found', observed: 'Python',
      expected: 'Python', weight: 0.3, source: { stage: 'scoring', rule_id: 'skills', rule_version: '1', evidence_source: 'description' },
      span: { field: 'description', start: 22, end: 28, text: 'Python', extractor_id: 'kw' } },
  ],
  conflicts: [], stage_trace: [{ stage: 'hard_filter', result: 'passed', detail: null }],
  exposure: { rank: 3, tier: 'excellent', final_score: 0.76 },
  summary: { supporting: 1, against: 0, unknown: 1, conflicts: 0, headline: 'Good match' },
  job: { title: 'Développeur Python', company: 'Acme', source: 'remoteok', location: 'Toulon' },
}

export const feedbackVocabulary: FeedbackVocabulary = {
  explicit_types: ['interested', 'not_interested', 'applied'],
  reasons: ['wrong_location', 'salary_too_low'],
  allowed_reasons: { not_interested: ['wrong_location', 'salary_too_low'] },
  canonical_statuses: ['saved', 'applied', 'interview', 'offer', 'rejected'],
}

export const feedbackEvents: FeedbackEvent[] = []

export const profile: Profile = {
  target_domains: ['software_development'], focus_domain: false, target_skills: ['Python'],
  target_skills_esco: [{ concept_uri: 'http://data.europa.eu/esco/skill/python', label: 'Python', keyword: 'Python', custom: false }],
  geography: {
    preferred_countries: ['France'], allowed_regions: [],
    location_preferences: [{ type: 'department', value: '83', status: 'preferred', label: 'Var' }],
  },
  work_modes: ['remote', 'hybrid'], contract_types: ['CDI'],
  experience: { preferred_levels: ['junior'], max_years: 3, excluded_seniority: ['senior'] },
  languages: ['French', 'English'], freshness_days: 30, max_education: 'bac+5',
  matching: { education_hard_constraint: false, education_preferred_penalty: 0.1, principal_skill_gate: true,
    mandatory_central_skill_gate: true, llm_extraction_enabled: false },
  looking_for: 'Un poste de développeur backend.',
}

export const departments: FrenchDepartment[] = [{ code: '83', name: 'Var' }, { code: '13', name: 'Bouches-du-Rhône' }]
export const countries: CountriesResponse = { total: 2, facets: [{ name: 'France', count: 10 }, { name: 'Germany', count: 2 }] }

export const jobStats: JobStats = {
  total_jobs: 640,
  skills: [{ name: 'Python', count: 120 }, { name: 'SQL', count: 90 }],
  tools: [{ name: 'Docker', count: 60 }],
  score_tiers: [{ tier: 'excellent', count: 20 }, { tier: 'strong', count: 50 }, { tier: 'low_priority', count: 300 }],
  rejections: [{ stage: 'hard_filter', detail: 'location', count: 200 }, { stage: 'requirements', detail: 'experience', count: 40 }],
  title_categories: [{ name: 'Backend', count: 80 }],
}

export const reviewQueue: ReviewQueuePage = {
  total: 1, limit: 50, offset: 0, items: [{ decision, job_stored: true, has_snapshot: true }],
}

export const skillGraph: SkillGraph = {
  window_days: 90, relevant_definition: 'accepted', n_jobs: 640, n_relevant: 120,
  nodes: [
    { skill: 'Python', demand: 120, relevant_demand: 80, relevant_share: 0.66, owned: true },
    { skill: 'Kubernetes', demand: 60, relevant_demand: 30, relevant_share: 0.5, owned: false },
  ],
  edges: [{ source: 'Python', target: 'Kubernetes', weight: 12 }],
  gaps: [{ skill: 'Kubernetes', relevant_demand: 30, demand: 60, relevant_share: 0.5,
    adjacent_owned: [{ skill: 'Python', jobs: 12 }], example_titles: ['DevOps Engineer'], why: 'Often asked with Python' }],
  owned: ['Python'], limitations: ['Skill names are matched literally'],
}

export const evalRuns: { total: number; unreadable: number; runs: EvalRunSummary[] } = {
  total: 1, unreadable: 0,
  runs: [{
    file: 'real-v1/r.json', run_id: 'r', created_at: NOW,
    dataset: { id: 'real-v1', version: '2', kind: 'real', jobs_sha256: 'j', labels_sha256: 'l' },
    strategy: { name: 'current', fingerprint: '2e1fd5b3c584aaaa' }, n_labelled: 250,
    overall: { tp: 7, tn: 115, fp: 116, fn: 3, borderline: 9, accuracy: 0.5, precision: 0.06, recall: 0.7, unsafe_rejection_rate: 0.3 },
    review_status: null, warnings: [],
  }],
}

export const labExperiments: { experiments: LabExperiment[] } = {
  experiments: [{
    experiment_id: 'exp-1', created_at: NOW, candidate_id: 'cand-1', dataset_id: 'real-v1', verdict: 'insufficient_evidence',
    eligible_for_approval: false, candidate_status: 'candidate', hypothesis: 'exclude defense', overrides: {},
    explanation: '4 discordant pairs', fixed: 4, broken: 0, guardrails: { no_new_unsafe_rejection: true },
    delta: { precision: 0.002 }, limitations: ['few labels'], promotion_evidence: 'not_eligible',
    independently_validated: false, evidence_basis: 'mixed',
  }],
}

export const labMemory: { items: LabMemoryItem[] } = {
  items: [{ item_id: 'm1', created_at: NOW, kind: 'observation', statement: 'Defense jobs are often false positives', status: null }],
}

export const activeStrategy: ActiveStrategy = {
  current: null, history: [], honoured_by_pipeline: true,
  pipeline: { status: 'default', reason: 'no approved bundle', pointer_id: null, bundle_hash: null, action: null,
    approved_by: null, approval_basis: null },
  note: 'config/ is used',
}

export const selfCheck: SelfCheckResponse = { latest: null, unreadable: 0, history: [] }

// `satisfies` (not a type annotation): gitleaks' linkedin-client-id rule reads
// "linkedInX: LinkedInStatus" as a client id.
export const linkedInScan = {
  id: 'li-1', started_at: EARLIER, completed_at: NOW, status: 'completed', jobs_found: 40, jobs_new: 12, error: null,
  stats: null,
} satisfies LinkedInScanRun

export const linkedInStatus = {
  enabled: true, waiting: 3, last_scan: linkedInScan, is_scanning: false, state: 'idle', progress: null,
} satisfies LinkedInStatus

export const helloWorkScan: HelloWorkScanRun = {
  id: 'hw-1', started_at: EARLIER, completed_at: NOW, status: 'completed', jobs_found: 30, jobs_new: 5, error: null,
  stats: { collector_errors: 0, collection_duration_seconds: 1700, metier_slugs_queried: 120 },
}

export const helloWorkStatus: HelloWorkStatus = {
  enabled: true, waiting: 0, last_scan: helloWorkScan, is_scanning: false, state: 'idle', progress: null,
}

/** The fixture for a GET to `path` (without the /api prefix), or undefined. */
export function resolveFixture(path: string): unknown {
  const p = path.replace(/^\/api/, '').split('?')[0]
  const routes: [RegExp, unknown][] = [
    [/^\/health$/, { status: 'ok' }],
    [/^\/dashboard$/, dashboard],
    [/^\/pipeline\/runs$/, [pipelineRun]],
    [/^\/pipeline\/runs\/[^/]+\/jobs$/, jobs],
    [/^\/pipeline\/runs\/[^/]+$/, pipelineRun],
    [/^\/jobs$/, jobs],
    [/^\/jobs\/countries$/, countries],
    [/^\/jobs\/stats$/, jobStats],
    [/^\/jobs\/feedback\/vocabulary$/, feedbackVocabulary],
    [/^\/jobs\/[^/]+\/explanation$/, explanation],
    [/^\/jobs\/[^/]+\/decision$/, decision],
    [/^\/jobs\/[^/]+\/feedback$/, feedbackEvents],
    [/^\/jobs\/[^/]+$/, job],
    [/^\/profile$/, profile],
    [/^\/departments(\/all)?$/, departments],
    [/^\/esco\/(skills|occupations)$/, []],
    [/^\/decisions\/review-queue$/, reviewQueue],
    [/^\/decisions\/[^/]+\/snapshot$/, { title: 'Développeur Python', company: 'Acme', location: 'Toulon', description: 'Texte' }],
    [/^\/skills\/graph$/, skillGraph],
    [/^\/lab\/runs$/, evalRuns],
    [/^\/lab\/experiments$/, labExperiments],
    [/^\/lab\/memory$/, labMemory],
    [/^\/lab\/active-strategy$/, activeStrategy],
    [/^\/lab\/self-check$/, selfCheck],
    [/^\/linkedin\/status$/, linkedInStatus],
    [/^\/linkedin\/scans$/, [linkedInScan]],
    [/^\/hellowork\/status$/, helloWorkStatus],
    [/^\/hellowork\/scans$/, [helloWorkScan]],
  ]
  return routes.find(([re]) => re.test(p))?.[1]
}
