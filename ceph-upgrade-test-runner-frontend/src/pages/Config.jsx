import { useEffect, useState } from 'react'

// Fixed 7-name allowlist -- mirrors dashboard/routes/test_runner.py's
// BASELINE_FILE_KEYS exactly. Kept in sync manually (no shared schema file
// between the Python backend and this Vite app); the backend is the source
// of truth and rejects anything not in its own list with a 400 regardless
// of what this array contains.
const BASELINE_FILE_KEYS = [
  'rbd_rep.sha256',
  'cephfs.sha256',
  's3_manifest.csv',
  'osd_crush_dump_before.json',
  'auth_list_before.txt',
  'config_dump_before.txt',
  'df_before.txt',
]

const TEST_GROUPS = ['A', 'B', 'C', 'D']
const PRIORITIES = ['P1', 'P2', 'P3']

function toggleInList(list, value) {
  return list.includes(value) ? list.filter((v) => v !== value) : [...list, value]
}

export default function Config() {
  const [nodes, setNodes] = useState([])
  const [baselineFiles, setBaselineFiles] = useState({})
  const [rgwZoneA, setRgwZoneA] = useState('')
  const [rgwZoneB, setRgwZoneB] = useState('')
  const [rgwVip, setRgwVip] = useState('')
  const [clientHost, setClientHost] = useState('')
  const [testGroups, setTestGroups] = useState([])
  const [priorities, setPriorities] = useState([])

  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState(null)
  const [saveStatus, setSaveStatus] = useState(null)
  const [sshChecking, setSshChecking] = useState(false)
  const [sshResults, setSshResults] = useState(null)
  const [uploadingKey, setUploadingKey] = useState(null)

  function applyConfig(data) {
    setNodes(data.nodes || [])
    setBaselineFiles(data.baseline_files || {})
    setRgwZoneA(data.rgw_endpoint_zone_a || '')
    setRgwZoneB(data.rgw_endpoint_zone_b || '')
    setRgwVip(data.rgw_endpoint_vip || '')
    setClientHost(data.client_host || '')
    setTestGroups(data.test_groups || [])
    setPriorities(data.priorities || [])
  }

  async function loadConfig() {
    setLoading(true)
    setLoadError(null)
    try {
      const res = await fetch('/api/test-runner/config')
      if (!res.ok) {
        throw new Error(`HTTP ${res.status}`)
      }
      const data = await res.json()
      applyConfig(data)
    } catch (err) {
      setLoadError(String(err))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    loadConfig()
  }, [])

  async function handleSshCheck() {
    setSshChecking(true)
    setSshResults(null)
    try {
      const res = await fetch('/api/test-runner/ssh-check', { method: 'POST' })
      const data = await res.json()
      setSshResults(data)
    } catch (err) {
      setSshResults({ __error: String(err) })
    } finally {
      setSshChecking(false)
    }
  }

  async function handleBaselineUpload(baselineKey, file) {
    if (!file) return
    setUploadingKey(baselineKey)
    try {
      const form = new FormData()
      form.append('file', file)
      const res = await fetch(`/api/test-runner/config/baseline/${encodeURIComponent(baselineKey)}`, {
        method: 'POST',
        body: form,
      })
      if (!res.ok) {
        throw new Error(`HTTP ${res.status}`)
      }
      const data = await res.json()
      applyConfig(data)
    } catch (err) {
      setLoadError(`Lỗi upload ${baselineKey}: ${err}`)
    } finally {
      setUploadingKey(null)
    }
  }

  async function handleSave() {
    setSaveStatus('saving')
    try {
      const res = await fetch('/api/test-runner/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          rgw_endpoint_zone_a: rgwZoneA,
          rgw_endpoint_zone_b: rgwZoneB,
          rgw_endpoint_vip: rgwVip,
          client_host: clientHost,
          test_groups: testGroups,
          priorities: priorities,
        }),
      })
      if (!res.ok) {
        throw new Error(`HTTP ${res.status}`)
      }
      const data = await res.json()
      applyConfig(data)
      setSaveStatus('saved')
    } catch (err) {
      setSaveStatus(`error: ${err}`)
    }
  }

  return (
    <div className="min-h-screen bg-slate-100 py-8 px-4">
      <div className="max-w-4xl mx-auto space-y-6">
        <div>
          <h1 className="text-2xl font-semibold text-slate-800">Cấu hình Test Runner</h1>
          <p className="mt-1 text-slate-500">
            Cấu hình baseline, endpoint RGW và phạm vi test cho Test Runner nâng cấp Ceph.
          </p>
        </div>

        {loadError && (
          <div className="rounded border border-red-300 bg-red-50 text-red-700 px-4 py-2 text-sm">
            {loadError}
          </div>
        )}

        {/* Node / role table (read-only) */}
        <section className="bg-white rounded shadow p-4">
          <h2 className="text-lg font-medium text-slate-800">Node cụm Ceph</h2>
          <p className="mt-1 text-sm text-slate-500">
            Danh sách node và role hiển thị dưới đây được lấy trực tiếp từ Dashboard. Để thay đổi
            node/SSH, vui lòng{' '}
            <a href="/settings" className="text-blue-600 underline">
              Cấu hình node/SSH trong Dashboard → Settings
            </a>
            .
          </p>

          {loading ? (
            <p className="mt-3 text-sm text-slate-400">Đang tải...</p>
          ) : nodes.length === 0 ? (
            <p className="mt-3 text-sm text-amber-600">Chưa cấu hình node nào trong Dashboard.</p>
          ) : (
            <table className="mt-3 w-full text-sm text-left border-collapse">
              <thead>
                <tr className="border-b border-slate-200 text-slate-500">
                  <th className="py-1 pr-4">Host</th>
                  <th className="py-1">Role</th>
                </tr>
              </thead>
              <tbody>
                {nodes.map((n) => (
                  <tr key={n.host} className="border-b border-slate-100">
                    <td className="py-1 pr-4 text-slate-800">{n.host}</td>
                    <td className="py-1 text-slate-600">{(n.roles || []).join(', ')}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          <button
            type="button"
            onClick={handleSshCheck}
            disabled={sshChecking || nodes.length === 0}
            className="mt-4 rounded bg-slate-800 text-white text-sm px-4 py-2 disabled:opacity-50"
          >
            {sshChecking ? 'Đang kiểm tra...' : 'Kiểm tra kết nối SSH'}
          </button>

          {sshResults && (
            <ul className="mt-3 space-y-1 text-sm">
              {sshResults.__error ? (
                <li className="text-red-600">{sshResults.__error}</li>
              ) : (
                Object.entries(sshResults).map(([host, ok]) => (
                  <li key={host} className="flex items-center gap-2">
                    <span
                      className={
                        'inline-block w-2.5 h-2.5 rounded-full ' + (ok ? 'bg-green-500' : 'bg-red-500')
                      }
                    />
                    <span className="text-slate-700">{host}</span>
                    <span className={ok ? 'text-green-600' : 'text-red-600'}>
                      {ok ? 'OK' : 'Không kết nối được'}
                    </span>
                  </li>
                ))
              )}
            </ul>
          )}
        </section>

        {/* Client host (Nhóm A: TC-RUN-001/010 cần 1 máy client riêng, không
            phải node cụm Ceph -- xem docs/ceph-upgrade-test-cases.md) */}
        <section className="bg-white rounded shadow p-4">
          <h2 className="text-lg font-medium text-slate-800">Máy client test I/O</h2>
          <p className="mt-1 text-sm text-slate-500">
            SSH host của máy client dùng cho test I/O liên tục trong lúc nâng cấp (Nhóm A) — đã map
            sẵn RBD image, mount CephFS, cấu hình aws cli. Không phải node MON/MGR/OSD/RGW ở trên.
          </p>
          <label className="mt-3 block text-sm text-slate-600">
            Client host
            <input
              type="text"
              value={clientHost}
              onChange={(e) => setClientHost(e.target.value)}
              placeholder="client01.lab.local"
              className="mt-1 w-full sm:w-80 rounded border border-slate-300 px-2 py-1"
            />
          </label>
        </section>

        {/* Baseline files */}
        <section className="bg-white rounded shadow p-4">
          <h2 className="text-lg font-medium text-slate-800">File baseline (7 file)</h2>
          <p className="mt-1 text-sm text-slate-500">
            Các file checksum/dump dùng để so sánh sau khi nâng cấp.
          </p>

          <div className="mt-3 space-y-3">
            {BASELINE_FILE_KEYS.map((key) => (
              <div key={key} className="flex items-center gap-3 text-sm">
                <span
                  className={
                    'inline-block w-2.5 h-2.5 rounded-full flex-shrink-0 ' +
                    (baselineFiles[key] ? 'bg-green-500' : 'bg-slate-300')
                  }
                />
                <span className="w-56 font-mono text-slate-700">{key}</span>
                <span className="w-28 text-slate-500">
                  {baselineFiles[key] ? 'Đã upload' : 'Chưa upload'}
                </span>
                <input
                  type="file"
                  disabled={uploadingKey === key}
                  onChange={(e) => handleBaselineUpload(key, e.target.files?.[0])}
                  className="text-xs"
                />
                {uploadingKey === key && <span className="text-slate-400">Đang upload...</span>}
              </div>
            ))}
          </div>
        </section>

        {/* RGW endpoints */}
        <section className="bg-white rounded shadow p-4">
          <h2 className="text-lg font-medium text-slate-800">Endpoint RGW (S3)</h2>
          <div className="mt-3 grid grid-cols-1 sm:grid-cols-3 gap-3">
            <label className="text-sm text-slate-600">
              Zone A
              <input
                type="text"
                value={rgwZoneA}
                onChange={(e) => setRgwZoneA(e.target.value)}
                placeholder="http://rgw-a:7480"
                className="mt-1 w-full rounded border border-slate-300 px-2 py-1"
              />
            </label>
            <label className="text-sm text-slate-600">
              Zone B
              <input
                type="text"
                value={rgwZoneB}
                onChange={(e) => setRgwZoneB(e.target.value)}
                placeholder="http://rgw-b:7480"
                className="mt-1 w-full rounded border border-slate-300 px-2 py-1"
              />
            </label>
            <label className="text-sm text-slate-600">
              VIP
              <input
                type="text"
                value={rgwVip}
                onChange={(e) => setRgwVip(e.target.value)}
                placeholder="http://rgw-vip:7480"
                className="mt-1 w-full rounded border border-slate-300 px-2 py-1"
              />
            </label>
          </div>
        </section>

        {/* Test group / priority selection */}
        <section className="bg-white rounded shadow p-4">
          <h2 className="text-lg font-medium text-slate-800">Phạm vi test</h2>

          <div className="mt-3">
            <p className="text-sm text-slate-600 mb-1">Nhóm test</p>
            <div className="flex gap-4">
              {TEST_GROUPS.map((g) => (
                <label key={g} className="flex items-center gap-1 text-sm text-slate-700">
                  <input
                    type="checkbox"
                    checked={testGroups.includes(g)}
                    onChange={() => setTestGroups((prev) => toggleInList(prev, g))}
                  />
                  Nhóm {g}
                </label>
              ))}
            </div>
          </div>

          <div className="mt-4">
            <p className="text-sm text-slate-600 mb-1">Độ ưu tiên</p>
            <div className="flex gap-4">
              {PRIORITIES.map((p) => (
                <label key={p} className="flex items-center gap-1 text-sm text-slate-700">
                  <input
                    type="checkbox"
                    checked={priorities.includes(p)}
                    onChange={() => setPriorities((prev) => toggleInList(prev, p))}
                  />
                  {p}
                </label>
              ))}
            </div>
          </div>
        </section>

        <div className="flex items-center gap-3">
          <button
            type="button"
            onClick={handleSave}
            className="rounded bg-blue-600 text-white text-sm px-4 py-2 disabled:opacity-50"
            disabled={saveStatus === 'saving'}
          >
            {saveStatus === 'saving' ? 'Đang lưu...' : 'Lưu cấu hình'}
          </button>
          {saveStatus === 'saved' && <span className="text-green-600 text-sm">Đã lưu.</span>}
          {saveStatus && saveStatus.startsWith('error') && (
            <span className="text-red-600 text-sm">{saveStatus}</span>
          )}
        </div>
      </div>
    </div>
  )
}
