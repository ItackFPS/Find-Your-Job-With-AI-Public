import { describe, it, expect, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ThemePicker } from '../ThemePicker'
import { THEME_STORAGE_KEY } from '../../theme/theme'

describe('ThemePicker', () => {
  beforeEach(() => window.localStorage.clear())

  it('applies and remembers the chosen theme', async () => {
    render(<ThemePicker />)
    const select = screen.getByLabelText('Theme')
    expect(select).toHaveValue('system')
    await userEvent.selectOptions(select, 'dark')
    expect(document.documentElement.dataset.theme).toBe('dark')
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe('dark')
    await userEvent.selectOptions(select, 'light')
    expect(document.documentElement.dataset.theme).toBe('light')
  })
})
