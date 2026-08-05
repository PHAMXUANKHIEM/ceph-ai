import { useState } from 'react'
import Config from './pages/Config.jsx'
import TestRunner from './pages/TestRunner.jsx'

// Story 10.6 adds the 2nd page (TestRunner) and this simple in-app nav.
// Deliberately a plain useState view switch, not react-router-dom --
// package.json has no routing library, and adding one for a 2-page app
// would stack a second stack exception on top of NFR15's existing one
// (React+Vite+Tailwind itself is already the one accepted deviation from
// the Dashboard's normal Jinja2+vanilla-JS stack).
const VIEWS = {
  config: { label: 'Cấu hình', component: Config },
  'test-runner': { label: 'Test Runner', component: TestRunner },
}

function App() {
  const [view, setView] = useState('test-runner')
  const ActiveView = VIEWS[view].component

  return (
    <div>
      <nav className="bg-slate-800 text-white px-4 py-2 flex gap-2">
        {Object.entries(VIEWS).map(([key, { label }]) => (
          <button
            key={key}
            type="button"
            onClick={() => setView(key)}
            className={
              'rounded px-3 py-1 text-sm ' + (view === key ? 'bg-slate-600' : 'hover:bg-slate-700')
            }
          >
            {label}
          </button>
        ))}
      </nav>
      <ActiveView />
    </div>
  )
}

export default App
