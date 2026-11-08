import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ToastProvider } from '../..'
import { AutostartSwitch } from '../AutostartSwitch'

const getAutostart = vi.fn()
const updateAutostart = vi.fn()
vi.mock('../../../api/client', async importOriginal => ({
  ...await importOriginal<typeof import('../../../api/client')>(),
  getAutostart: () => getAutostart(),
  updateAutostart: (enabled: boolean) => updateAutostart(enabled),
}))

function show() {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <ToastProvider><AutostartSwitch /></ToastProvider>
    </QueryClientProvider>,
  )
}

describe('AutostartSwitch', () => {
  beforeEach(() => { getAutostart.mockReset(); updateAutostart.mockReset() })

  it('turns Start with Windows on', async () => {
    getAutostart.mockResolvedValue({ supported: true, enabled: false })
    updateAutostart.mockResolvedValue({ supported: true, enabled: true })
    show()
    await userEvent.click(await screen.findByRole('switch', { name: /Start with Windows/ }))
    await waitFor(() => expect(updateAutostart).toHaveBeenCalledWith(true))
  })

  it('is hidden where it cannot work', async () => {
    getAutostart.mockResolvedValue({ supported: false, enabled: false })
    const { container } = show()
    await waitFor(() => expect(getAutostart).toHaveBeenCalled())
    expect(container.textContent).toBe('')
  })
})
