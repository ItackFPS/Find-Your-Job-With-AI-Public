import '@testing-library/jest-dom/vitest'
import { afterEach } from 'vitest'
import { cleanup } from '@testing-library/react'

// `globals: false` in vite.config.ts means @testing-library/react's
// automatic afterEach(cleanup) registration (which detects test-framework
// globals) never fires — register it explicitly instead, so each test
// starts from an empty document.
afterEach(() => {
  cleanup()
})
