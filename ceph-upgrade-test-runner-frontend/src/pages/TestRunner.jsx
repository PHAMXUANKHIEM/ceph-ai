import { useEffect, useRef, useState } from 'react'

// Same plain setInterval + fetch convention as the rest of this project's
// live-progress pages (dashboard/static/deploy_cluster.js,
// volume_perf_sweep.js, restore_cluster.js) -- no WebSocket, dashboard/ws.py
// only ever serves the original Incident feed (Story 1.5). 3000ms matches
// volume_perf_sweep.js's POLL_INTERVAL_MS.
const POLL_INTERVAL_MS = 3000

const GROUPS = ['A', 'B', 'C', 'D', 'E']

const STATUS_STYLES = {
  not_started: 'bg-slate-200 text-slate-600',
  running: 'bg-blue-100 text-blue-700',
  pass: 'bg-green-100 text-green-700',
  fail: 'bg-red-100 text-red-700',
  error: 'bg-orange-100 text-orange-700',
  skip: 'bg-amber-100 text-amber-700',
}

const STATUS_LABELS = {
  not_started: 'Chưa chạy',
  running: 'Đang chạy',
  pass: 'Đạt',
  fail: 'Không đạt',
  error: 'Lỗi',
  skip: 'Bỏ qua',
}

function StatusBadge({ status, overridden }) {
  const cls = STATUS_STYLES[status] || 'bg-slate-200 text-slate-600'
  return (
    <span className={'inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium ' + cls}>
      {STATUS_LABELS[status] || status}
      {overridden && <span title="Đã ghi đè thủ công">✋</span>}
    </span>
  )
}

function isTerminal(status) {
  return status === 'pass' || status === 'fail' || status === 'error' || status === 'skip'
}

export default function TestRunner() {
  const [tests, setTests] = useState([])
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState(null)
  const [groupFilter, setGroupFilter] = useState('ALL')

  const [expandedId, setExpandedId] = useState(null)
  const [details, setDetails] = useState({}) // id -> detail object
  const [runningAction, setRunningAction] = useState(null) // id currently being POSTed to /run
  const [overrideNote, setOverrideNote] = useState('')
  const [overrideStatus, setOverrideStatus] = useState(null) // id -> 'saving' | 'error: ...'
  const [copySummaryStatus, setCopySummaryStatus] = useState(null) // null | 'copying' | 'copied' | 'error: ...'

  const detailPollRef = useRef(null)

  async function loadTests() {
    try {
      const res = await fetch('/api/test-runner/tests')
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const data = await res.json()
      setTests(data.tests || [])
      setLoadError(null)
    } catch (err) {
      setLoadError(String(err))
    } finally {
      setLoading(false)
    }
  }

  async function loadDetail(id) {
    try {
      const res = await fetch(`/api/test-runner/tests/${encodeURIComponent(id)}/result`)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const data = await res.json()
      setDetails((prev) => ({ ...prev, [id]: data }))
      // keep the summary row's status in sync with what /result just polled,
      // without waiting for the next list-wide refresh
      setTests((prev) => prev.map((t) => (t.id === id ? { ...t, status: data.status, overridden: data.overridden } : t)))
    } catch (err) {
      setDetails((prev) => ({ ...prev, [id]: { ...(prev[id] || {}), __error: String(err) } }))
    }
  }

  useEffect(() => {
    loadTests()
    const timer = setInterval(loadTests, POLL_INTERVAL_MS)
    return () => clearInterval(timer)
  }, [])

  // Poll the expanded row's own detail endpoint while it's open -- each GET
  // is what actually drives a background test's poll_test_case() tick
  // server-side (see dashboard/routes/test_runner.py), so this IS the
  // "caller responsible for calling poll() repeatedly" loop framework.py
  // documents.
  useEffect(() => {
    if (detailPollRef.current) {
      clearInterval(detailPollRef.current)
      detailPollRef.current = null
    }
    if (!expandedId) return undefined

    loadDetail(expandedId)
    detailPollRef.current = setInterval(() => {
      const current = details[expandedId]
      if (current && isTerminal(current.status)) return
      loadDetail(expandedId)
    }, POLL_INTERVAL_MS)

    return () => {
      if (detailPollRef.current) {
        clearInterval(detailPollRef.current)
        detailPollRef.current = null
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [expandedId])

  async function handleRun(id) {
    setRunningAction(id)
    try {
      const res = await fetch(`/api/test-runner/tests/${encodeURIComponent(id)}/run`, { method: 'POST' })
      if (!res.ok && res.status !== 409) {
        throw new Error(`HTTP ${res.status}`)
      }
      await loadTests()
      if (expandedId === id) {
        await loadDetail(id)
      }
    } catch (err) {
      setLoadError(`Lỗi khi chạy ${id}: ${err}`)
    } finally {
      setRunningAction(null)
    }
  }

  function toggleExpand(id) {
    setExpandedId((prev) => (prev === id ? null : id))
    setOverrideNote('')
  }

  async function handleOverride(id, status) {
    setOverrideStatus('saving')
    try {
      const res = await fetch(`/api/test-runner/tests/${encodeURIComponent(id)}/override`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ status, note: overrideNote }),
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const data = await res.json()
      setDetails((prev) => ({ ...prev, [id]: data }))
      setTests((prev) => prev.map((t) => (t.id === id ? { ...t, status: data.status, overridden: true } : t)))
      setOverrideStatus(null)
      setOverrideNote('')
    } catch (err) {
      setOverrideStatus(`error: ${err}`)
    }
  }

  // Story 10.7: report export -- same-origin GET with Content-Disposition:
  // attachment, so a plain navigation triggers the browser's normal download
  // UI with zero client-side blob handling needed.
  function handleDownloadMarkdown() {
    window.location.href = '/api/test-runner/report/markdown'
  }

  function handleDownloadExcel() {
    window.location.href = '/api/test-runner/report/excel'
  }

  async function handleCopySummary() {
    setCopySummaryStatus('copying')
    try {
      const res = await fetch('/api/test-runner/report/summary')
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const data = await res.json()
      await navigator.clipboard.writeText(data.summary_text || '')
      setCopySummaryStatus('copied')
      setTimeout(() => setCopySummaryStatus(null), 2000)
    } catch (err) {
      setCopySummaryStatus(`error: ${err}`)
    }
  }

  const visibleTests = groupFilter === 'ALL' ? tests : tests.filter((t) => t.group === groupFilter)

  const summary = tests.reduce(
    (acc, t) => {
      acc.total += 1
      acc[t.status] = (acc[t.status] || 0) + 1
      return acc
    },
    { total: 0 }
  )

  return (
    <div className="min-h-screen bg-slate-100 py-8 px-4">
      <div className="max-w-5xl mx-auto space-y-6">
        <div>
          <h1 className="text-2xl font-semibold text-slate-800">Test Runner</h1>
          <p className="mt-1 text-slate-500">
            Danh sách test case nâng cấp Ceph — chạy, theo dõi tiến trình/log trực tiếp, và ghi đè kết
            quả thủ công khi cần.
          </p>
        </div>

        {loadError && (
          <div className="rounded border border-red-300 bg-red-50 text-red-700 px-4 py-2 text-sm">
            {loadError}
          </div>
        )}

        {/* Aggregate summary + report export */}
        <section className="bg-white rounded shadow p-4 space-y-3">
          <div className="flex flex-wrap gap-4 text-sm">
            <span className="font-medium text-slate-800">Tổng: {summary.total}</span>
            <span className="text-slate-500">Chưa chạy: {summary.not_started || 0}</span>
            <span className="text-blue-600">Đang chạy: {summary.running || 0}</span>
            <span className="text-green-600">Đạt: {summary.pass || 0}</span>
            <span className="text-red-600">Không đạt: {summary.fail || 0}</span>
            <span className="text-orange-600">Lỗi: {summary.error || 0}</span>
            <span className="text-amber-600">Bỏ qua: {summary.skip || 0}</span>
          </div>
          <div className="flex flex-wrap items-center gap-2 border-t border-slate-100 pt-3">
            <button
              type="button"
              onClick={handleDownloadMarkdown}
              className="rounded bg-slate-700 text-white text-xs px-3 py-1.5"
            >
              Tải Markdown
            </button>
            <button
              type="button"
              onClick={handleDownloadExcel}
              className="rounded bg-slate-700 text-white text-xs px-3 py-1.5"
            >
              Tải Excel
            </button>
            <button
              type="button"
              onClick={handleCopySummary}
              disabled={copySummaryStatus === 'copying'}
              className="rounded border border-slate-300 text-slate-700 text-xs px-3 py-1.5 disabled:opacity-50"
            >
              {copySummaryStatus === 'copying' ? 'Đang copy...' : 'Copy Summary'}
            </button>
            {copySummaryStatus === 'copied' && <span className="text-xs text-green-600">Đã copy</span>}
            {copySummaryStatus && copySummaryStatus.startsWith('error') && (
              <span className="text-xs text-red-600">{copySummaryStatus}</span>
            )}
          </div>
        </section>

        {/* Group filter (client-side only -- the list itself is already
            server-filtered by the saved Config test_groups/priorities
            selection) */}
        <div className="flex gap-2">
          {['ALL', ...GROUPS].map((g) => (
            <button
              key={g}
              type="button"
              onClick={() => setGroupFilter(g)}
              className={
                'rounded px-3 py-1 text-sm ' +
                (groupFilter === g ? 'bg-slate-800 text-white' : 'bg-white text-slate-600 border border-slate-300')
              }
            >
              {g === 'ALL' ? 'Tất cả' : `Nhóm ${g}`}
            </button>
          ))}
        </div>

        {/* Test list */}
        <section className="bg-white rounded shadow divide-y divide-slate-100">
          {loading ? (
            <p className="p-4 text-sm text-slate-400">Đang tải...</p>
          ) : loadError ? null : visibleTests.length === 0 ? (
            <p className="p-4 text-sm text-amber-600">
              Không có test case nào — kiểm tra lựa chọn Nhóm/Ưu tiên ở trang Cấu hình.
            </p>
          ) : (
            visibleTests.map((t) => {
              const isExpanded = expandedId === t.id
              const detail = details[t.id]
              return (
                <div key={t.id}>
                  <div className="flex items-center gap-3 p-3">
                    <button
                      type="button"
                      onClick={() => toggleExpand(t.id)}
                      className="flex-1 text-left"
                    >
                      <div className="flex items-center gap-2">
                        <span className="font-mono text-xs text-slate-500 w-28">{t.id}</span>
                        <span className="text-sm text-slate-800">{t.name}</span>
                        {t.background && (
                          <span className="text-xs text-slate-400" title="Test nền/dài hạn">
                            ⏳
                          </span>
                        )}
                      </div>
                    </button>
                    <span className="text-xs text-slate-400">
                      {t.group} · {t.priority}
                    </span>
                    <StatusBadge status={t.status} overridden={t.overridden} />
                    <button
                      type="button"
                      onClick={() => handleRun(t.id)}
                      disabled={t.status === 'running' || runningAction === t.id}
                      className="rounded bg-blue-600 text-white text-xs px-3 py-1 disabled:opacity-50"
                    >
                      {runningAction === t.id ? '...' : 'Chạy'}
                    </button>
                  </div>

                  {isExpanded && (
                    <div className="bg-slate-50 px-4 py-3 text-sm space-y-3">
                      {!detail ? (
                        <p className="text-slate-400">Đang tải chi tiết...</p>
                      ) : detail.__error ? (
                        <p className="text-red-600">{detail.__error}</p>
                      ) : (
                        <>
                          {detail.criteria && detail.criteria.length > 0 && (
                            <div>
                              <p className="text-xs font-medium text-slate-500 mb-1">Tiêu chí</p>
                              <ul className="space-y-1">
                                {detail.criteria.map((c, idx) => (
                                  <li key={idx} className="flex items-start gap-2">
                                    <span
                                      className={
                                        'inline-block w-2.5 h-2.5 rounded-full flex-shrink-0 mt-1 ' +
                                        (c.passed === true
                                          ? 'bg-green-500'
                                          : c.passed === false
                                          ? 'bg-red-500'
                                          : 'bg-slate-300')
                                      }
                                    />
                                    <span className="text-slate-700">
                                      {c.description}
                                      {c.detail && <span className="text-slate-400"> — {c.detail}</span>}
                                    </span>
                                  </li>
                                ))}
                              </ul>
                            </div>
                          )}

                          {detail.notes && (
                            <p className="text-slate-600">
                              <span className="font-medium">Ghi chú engine: </span>
                              {detail.notes}
                            </p>
                          )}

                          {detail.overridden && (
                            <p className="text-slate-700 bg-amber-50 border border-amber-200 rounded px-2 py-1">
                              <span className="font-medium">Đã ghi đè thủ công: </span>
                              {detail.override_note || '(không có ghi chú)'}
                            </p>
                          )}

                          {detail.raw_output && (
                            <div>
                              <p className="text-xs font-medium text-slate-500 mb-1">Log</p>
                              <pre className="bg-slate-900 text-slate-100 text-xs rounded p-2 max-h-64 overflow-auto whitespace-pre-wrap">
                                {detail.raw_output}
                              </pre>
                            </div>
                          )}

                          {!detail.overridden && (
                            <div className="border-t border-slate-200 pt-3">
                              <p className="text-xs font-medium text-slate-500 mb-1">
                                Ghi đè kết quả thủ công
                              </p>
                              <div className="flex items-center gap-2">
                                <input
                                  type="text"
                                  value={overrideNote}
                                  onChange={(e) => setOverrideNote(e.target.value)}
                                  placeholder="Ghi chú (tuỳ chọn)"
                                  className="flex-1 rounded border border-slate-300 px-2 py-1 text-sm"
                                />
                                <button
                                  type="button"
                                  onClick={() => handleOverride(t.id, 'pass')}
                                  className="rounded bg-green-600 text-white text-xs px-3 py-1"
                                >
                                  Đánh dấu Đạt
                                </button>
                                <button
                                  type="button"
                                  onClick={() => handleOverride(t.id, 'fail')}
                                  className="rounded bg-red-600 text-white text-xs px-3 py-1"
                                >
                                  Đánh dấu Không đạt
                                </button>
                              </div>
                              {overrideStatus === 'saving' && (
                                <p className="text-xs text-slate-400 mt-1">Đang lưu...</p>
                              )}
                              {overrideStatus && overrideStatus.startsWith('error') && (
                                <p className="text-xs text-red-600 mt-1">{overrideStatus}</p>
                              )}
                            </div>
                          )}
                        </>
                      )}
                    </div>
                  )}
                </div>
              )
            })
          )}
        </section>
      </div>
    </div>
  )
}
