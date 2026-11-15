import { useState } from 'react'
import { describe, it, expect, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { EscoEntry } from '../../api/types'
import { DomainPicker } from '../Profile'

const NURSE = {
  concept_uri: 'http://data.europa.eu/esco/occupation/nurse',
  label: 'nurse responsible for general care',
  label_fr: 'infirmier responsable des soins généraux',
  keyword: 'nurse',
  alt_labels_en: [],
  alt_labels_fr: [],
  concept_type: 'occupation',
  is_esco: true,
}

vi.mock('axios', () => {
  const client = {
    get: async (url: string) => ({ data: url.includes('/esco/occupations') ? [NURSE] : [] }),
    post: async () => ({ data: {} }),
    put: async () => ({ data: {} }),
    patch: async () => ({ data: {} }),
  }
  return { default: { create: () => client } }
})

function Harness() {
  const [domains, setDomains] = useState<string[]>([])
  const [occupations, setOccupations] = useState<EscoEntry[]>([])
  return (
    <>
      <DomainPicker values={domains} onChange={setDomains} occupations={occupations} onOccupationsChange={setOccupations} />
      <output data-testid="domains">{JSON.stringify(domains)}</output>
      <output data-testid="occupations">{JSON.stringify(occupations)}</output>
    </>
  )
}

describe('DomainPicker occupations', () => {
  it('records an ESCO pick as a domain and as an occupation with its French label, and removes both together', async () => {
    const user = userEvent.setup()
    render(<Harness />)

    await user.type(screen.getByRole('textbox'), 'nurse')
    await user.click(await screen.findByText(NURSE.label))

    expect(screen.getByTestId('domains').textContent).toBe('["nurse"]')
    const [entry] = JSON.parse(screen.getByTestId('occupations').textContent ?? '[]') as EscoEntry[]
    expect(entry).toEqual({
      concept_uri: NURSE.concept_uri,
      label: NURSE.label,
      keyword: 'nurse',
      custom: false,
      label_fr: NURSE.label_fr,
    })

    await user.click(screen.getByRole('button', { name: /nurse/i }))
    await waitFor(() => expect(screen.getByTestId('domains').textContent).toBe('[]'))
    expect(screen.getByTestId('occupations').textContent).toBe('[]')
  })

  it('records no occupation for custom text', async () => {
    const user = userEvent.setup()
    render(<Harness />)

    await user.type(screen.getByRole('textbox'), 'zzz-custom{Enter}')

    expect(screen.getByTestId('domains').textContent).toBe('["zzz-custom"]')
    expect(screen.getByTestId('occupations').textContent).toBe('[]')
  })
})
