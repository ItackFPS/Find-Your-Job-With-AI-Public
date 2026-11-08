import { describe, it, expect, vi, beforeEach } from 'vitest'
import { useState } from 'react'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { DomainPicker } from '../Profile'

const net: { rome: unknown[]; esco: unknown[]; asked: string[] } = { rome: [], esco: [], asked: [] }

vi.mock('axios', () => {
  const client = {
    get: async (url: string) => {
      net.asked.push(url)
      if (url === '/rome/occupations') return { data: net.rome }
      if (url === '/esco/occupations') return { data: net.esco }
      return { data: [] }
    },
    post: async () => ({ data: {} }), put: async () => ({ data: {} }), patch: async () => ({ data: {} }),
  }
  return { default: { create: () => client } }
})

const rome = (code: string, label: string, keyword: string, kind = 'occupation') =>
  ({ code, label, keyword, kind, group: '', is_rome: true })

function Harness({ initial = [] as string[] }) {
  const [values, setValues] = useState<string[]>(initial)
  return (
    <>
      <DomainPicker values={values} onChange={setValues} />
      <output data-testid="values">{JSON.stringify(values)}</output>
    </>
  )
}

beforeEach(() => { net.rome = []; net.esco = []; net.asked = [] })

describe('DomainPicker ROME suggestions', () => {
  it('suggests ROME entries for any sector and stores the keyword, not the long label', async () => {
    net.rome = [rome('M1203', 'Comptable', 'Comptable'),
      rome('A1416', "Exploitant / Exploitante agricole", 'Exploitant agricole')]
    render(<Harness />)
    await userEvent.type(screen.getByRole('textbox'), 'agri')
    const row = await screen.findByText('Exploitant / Exploitante agricole')
    expect(screen.getAllByText('ROME').length).toBeGreaterThan(0)
    await userEvent.click(row)
    expect(screen.getByTestId('values')).toHaveTextContent('["Exploitant agricole"]')
  })

  it('does not suggest a ROME entry whose keyword is already picked, or one repeating an ESCO row', async () => {
    net.rome = [rome('M1203', 'Comptable', 'Comptable'), rome('M1204', 'Comptable unique', 'Comptable unique')]
    net.esco = [{ concept_uri: 'http://x', label: 'comptable unique', label_fr: '', keyword: 'comptable unique',
      alt_labels_en: [], alt_labels_fr: [], concept_type: 'occupation', is_esco: true }]
    render(<Harness initial={['comptable']} />)
    await userEvent.type(screen.getByRole('textbox'), 'compt')
    await screen.findByText('Comptable unique')
    expect(screen.queryByText('Comptable')).not.toBeInTheDocument()   // already picked (any case)
    expect(screen.getAllByText(/omptable unique/i)).toHaveLength(1)   // ROME row only, no ESCO duplicate
  })

  it('still works when the ROME index is not set up or the request fails', async () => {
    net.rome = []
    net.esco = [{ concept_uri: 'http://x', label: 'Seller', label_fr: '', keyword: 'Seller',
      alt_labels_en: [], alt_labels_fr: [], concept_type: 'occupation', is_esco: true }]
    render(<Harness />)
    await userEvent.type(screen.getByRole('textbox'), 'sell')
    expect(await screen.findByText('Seller')).toBeInTheDocument()
    expect(net.asked).toContain('/rome/occupations')
  })
})
