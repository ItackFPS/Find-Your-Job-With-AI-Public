import { describe, it, expect } from 'vitest'
import { PIPELINE_STAGES, findPipelineStage, stageLabel } from '../pipelineStages'
import type { RunStats } from '../../api/client'

function baseStats(overrides: Partial<RunStats> = {}): RunStats {
  return {
    collected: 0,
    validated: 0,
    deduplicated: 0,
    filtered: 0,
    scored: 0,
    semantically_matched: 0,
    personalized: 0,
    duration_seconds: 0,
    by_source: {},
    by_country: {},
    by_work_mode: {},
    errors: [],
    profile_hash: '',
    model_name: '',
    scoring_config_hash: '',
    current_stage: '',
    ...overrides,
  }
}

describe('PIPELINE_STAGES', () => {
  it('includes llm_eligibility between requirements_gate and score', () => {
    const keys = PIPELINE_STAGES.map(s => s.key)
    const rgIndex = keys.indexOf('requirements_gate')
    const eligIndex = keys.indexOf('llm_eligibility')
    const scoreIndex = keys.indexOf('score')
    expect(eligIndex).toBeGreaterThan(-1)
    expect(eligIndex).toBeGreaterThan(rgIndex)
    expect(eligIndex).toBeLessThan(scoreIndex)
  })

  it('has no duplicate stage keys', () => {
    const keys = PIPELINE_STAGES.map(s => s.key)
    expect(new Set(keys).size).toBe(keys.length)
  })

  it('matches the backend orchestrator stage order', () => {
    // Mirrors src/pipeline/orchestrator.py's stage sequence — see that
    // module's docstring. Update both places together if the order changes.
    expect(PIPELINE_STAGES.map(s => s.key)).toEqual([
      'collect', 'validate', 'deduplicate', 'filter', 'requirements_gate',
      'llm_eligibility', 'score', 'phase2', 'semantic', 'personalize',
    ])
  })

  it("llm_eligibility's input/done accessors read the matching RunStats fields", () => {
    const stage = findPipelineStage('llm_eligibility')!
    const stats = baseStats({ llm_eligibility_input: 120, llm_eligibility_passed: 97 })
    expect(stage.input(stats)).toBe(120)
    expect(stage.done(stats)).toBe(97)
  })

  it('every stage input/done accessor runs without throwing on minimal stats', () => {
    const stats = baseStats()
    for (const stage of PIPELINE_STAGES) {
      expect(() => stage.input(stats)).not.toThrow()
      expect(() => stage.done(stats)).not.toThrow()
    }
  })
})

describe('findPipelineStage', () => {
  it('returns the stage definition for a known key', () => {
    expect(findPipelineStage('filter')?.label).toBe('Hard Filter')
  })

  it('returns undefined for an unknown key', () => {
    expect(findPipelineStage('not_a_real_stage')).toBeUndefined()
  })
})

describe('stageLabel', () => {
  it('returns the activity-phrase label for a known stage', () => {
    expect(stageLabel('llm_eligibility')).toBe('Checking eligibility (AI)')
    expect(stageLabel('requirements_gate')).toBe('Checking requirements')
  })

  it('returns a label for the preflight pseudo-stage even though it has no funnel row', () => {
    expect(findPipelineStage('preflight')).toBeUndefined()
    expect(stageLabel('preflight')).toBe('Preparing run')
  })

  it('falls back to "Working" for an empty stage', () => {
    expect(stageLabel('')).toBe('Working')
  })

  it('falls back to the raw key for an unrecognized stage', () => {
    expect(stageLabel('some_future_stage')).toBe('some_future_stage')
  })
})
