import Config from './pages/Config.jsx'

// Story 10.2 adds the first real page (Config). Full page routing/nav
// across multiple Test Runner screens (dashboard/log view, report, etc.)
// is Story 10.6's job -- until then this is a single-page app that just
// renders Config directly, no router library needed for one page.
function App() {
  return <Config />
}

export default App
