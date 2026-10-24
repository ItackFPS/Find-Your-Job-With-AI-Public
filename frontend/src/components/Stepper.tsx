import type { ReactNode } from 'react'

export interface StepperStep {
  key: string
  label: ReactNode
}

export interface StepperProps {
  steps: StepperStep[]
  /** Index of the current step; earlier steps show as complete. */
  current: number
  /** Accessible name of the list, e.g. "Profile setup". */
  label: string
}

/**
 * A compact, read-only progress indicator for multi-step flows (setup wizard, application
 * status). The current step is announced with aria-current="step".
 */
export function Stepper({ steps, current, label }: StepperProps) {
  return (
    <ol className="fyj-stepper" aria-label={label}>
      {steps.map((step, i) => {
        const state = i < current ? 'complete' : i === current ? 'current' : 'upcoming'
        return (
          <li
            key={step.key}
            className={`fyj-stepper__step fyj-stepper__step--${state}`}
            aria-current={state === 'current' ? 'step' : undefined}
          >
            <span className="fyj-stepper__marker" aria-hidden="true">{state === 'complete' ? '✓' : i + 1}</span>
            <span>{step.label}</span>
          </li>
        )
      })}
    </ol>
  )
}
