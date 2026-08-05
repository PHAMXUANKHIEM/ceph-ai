import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
//
// This React app lives alongside ceph-aiops's FastAPI + Jinja2 dashboard
// (dashboard/app.py) rather than shipping its own backend — Epic 10's Test
// Runner UI calls the SAME dashboard API (see dashboard/routes/
// test_runner.py) that every other page in this project uses, so there is
// only ever one backend process. dashboard/app.py runs on port 8000 (see
// README.md's `uvicorn dashboard.app:app --port 8000`). Proxying /api here
// means the Vite dev server (port 5173) and the FastAPI backend can talk to
// each other in dev without standing up CORS on the FastAPI side.
//
// Target is read from DASHBOARD_HOST/DASHBOARD_PORT (same env vars
// scripts/deploy/restart_services.sh uses to launch uvicorn) rather than
// hardcoded to localhost:8000 -- a deploy can override DASHBOARD_HOST to a
// specific bind address (see restart_services.sh's deploy.local.env, e.g.
// DASHBOARD_HOST=103.69.193.220) instead of 0.0.0.0, in which case the
// backend isn't reachable on localhost at all and this proxy would
// ECONNREFUSED on every /api call -- surfacing in the Test Runner UI as a
// misleading "Không có test case nào" empty state that looks like a
// Group/Priority filter problem instead of a connectivity one.
const dashboardHost = process.env.DASHBOARD_HOST || 'localhost'
const dashboardPort = process.env.DASHBOARD_PORT || '8000'

export default defineConfig({
  plugins: [react()],
  server: {
    host: '0.0.0.0',
    port: 5173,
    proxy: {
      '/api': {
        target: `http://${dashboardHost}:${dashboardPort}`,
        changeOrigin: true,
      },
    },
  },
})
