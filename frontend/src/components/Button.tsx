import { forwardRef } from 'react'
import type { ButtonHTMLAttributes } from 'react'

export type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'danger'

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  /** primary: the one main action of a view · secondary: other actions · ghost: low-emphasis
   *  actions in dense places · danger: destructive actions. */
  variant?: ButtonVariant
  fullWidth?: boolean
}

/**
 * The design-system button: at least 44 x 44 px (a WCAG 2.5.5 target), a visible focus ring
 * from the global :focus-visible rule, and colours from the theme tokens
 * (styles/components.css). Defaults to type="button" so it never submits a form by accident.
 */
export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = 'secondary', fullWidth = false, className, type = 'button', ...rest },
  ref,
) {
  const classes = ['fyj-btn', `fyj-btn--${variant}`, fullWidth ? 'fyj-btn--full' : '', className ?? '']
    .filter(Boolean)
    .join(' ')
  return <button ref={ref} type={type} className={classes} {...rest} />
})
