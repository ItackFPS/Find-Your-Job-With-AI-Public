import { Navigate, Routes, Route, useLocation } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import Dashboard from './pages/Dashboard'
import Jobs from './pages/Jobs'
import JobDetail from './pages/JobDetail'
import Profile from './pages/Profile'
import Pipeline from './pages/Pipeline'
import Stats from './pages/Stats'
import Review from './pages/Review'
import Skills from './pages/Skills'
import Lab from './pages/Lab'
import Quality from './pages/Quality'
import Settings from './pages/Settings'
import DesignSystem from './pages/DesignSystem'
import Onboarding from './pages/Onboarding'
import { ONBOARDING_QUERY_KEY } from './components/settings'
import { getOnboarding } from './api/client'
import { Sidebar } from './components'

export default function App() {
  // Subscribing here re-renders the whole tree when the language changes, so labels
  // computed outside components (stage names, scanner labels) follow it too.
  useTranslation()
  const { pathname } = useLocation()
  const inSetup = pathname === '/welcome'
  // A backend that cannot answer sends nobody anywhere: the pages show their own error.
  const { data: onboarding } = useQuery({ queryKey: ONBOARDING_QUERY_KEY, queryFn: getOnboarding, retry: false })
  if (onboarding && !onboarding.completed && !inSetup) return <Navigate to="/welcome" replace />
  return (
    <div style={{ minHeight: '100vh', display: 'flex' }}>
      {!inSetup && <Sidebar />}

      <main style={{ flex: 1, padding: '1.5rem 2.5rem', width: '100%', boxSizing: 'border-box' }}>
        <Routes>
          <Route path="/welcome" element={<Onboarding />} />
          <Route path="/" element={<Dashboard />} />
          <Route path="/pipeline" element={<Pipeline />} />
          <Route path="/jobs" element={<Jobs />} />
          <Route path="/jobs/:id" element={<JobDetail />} />
          <Route path="/stats" element={<Stats />} />
          <Route path="/review" element={<Review />} />
          <Route path="/skills" element={<Skills />} />
          <Route path="/quality" element={<Quality />} />
          <Route path="/lab" element={<Lab />} />
          <Route path="/profile" element={<Profile />} />
          <Route path="/settings" element={<Settings />} />
          <Route path="/design" element={<DesignSystem />} />
        </Routes>
      </main>
    </div>
  )
}
