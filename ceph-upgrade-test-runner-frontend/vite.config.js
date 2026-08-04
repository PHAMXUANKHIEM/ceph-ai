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
export default defineConfig({
  plugins: [react()],
  server: {
    host: '0.0.0.0',
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
})
