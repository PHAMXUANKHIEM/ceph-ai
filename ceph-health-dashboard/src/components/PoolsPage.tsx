import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Brush,
  Activity,
  Database,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Grid3X3,
  HardDrive,
  Info,
  Layers3,
  Pencil,
  Plus,
  Search,
  RefreshCw,
  Shield,
  ShieldOff,
  TrendingUp,
  X,
} from "lucide-react";
import { EmptyState } from "./EmptyState";
import { ErrorState } from "./ErrorState";
import { PageHeader } from "./PageHeader";
import { useClusterSnapshotEvents, type SnapshotEvent } from "../useClusterSnapshotEvents";
import { getSnapshotState, SNAPSHOT_STATE_LABEL } from "../snapshotState";

type PoolRow = {
  name: string;
  redundancy: string;
  pgs: number;
  crush_rule: string;
  used: string;
  used_bytes?: number | null;
  total_bytes?: number | null;
  objects: number;
  read_iops: number;
  write_iops: number;
  size: number | null;
  protected: boolean;
};

type PoolsBootstrap = {
  pools: PoolRow[];
  selectedPool?: string | null;
  updatedAt?: string | null;
  snapshotMeta?: SnapshotMeta;
  isAdmin: boolean;
  clusterId: string;
  createSuccess?: boolean;
  actionSuccess?: string | null;
  queryError?: string | null;
};

type SnapshotMeta = {
  generation?: number;
  collected_at?: string | null;
  age_seconds?: number | null;
  stale?: boolean;
  available?: boolean;
  last_error?: string | null;
};

const POOLS_PER_PAGE = 25;

function ToolbarButton({ icon: Icon, label, danger = false, disabled = false, onClick }: { icon: React.ElementType; label: string; danger?: boolean; disabled?: boolean; onClick?: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className={danger
        ? "btn btn-danger-outline btn-sm"
        : "btn btn-ghost btn-sm"}
    >
      <Icon size={16} strokeWidth={1.8} aria-hidden="true" />
      {label}
    </button>
  );
}

export function PoolsPage({ bootstrap }: { bootstrap: PoolsBootstrap }) {
  const [rows, setRows] = useState<PoolRow[]>(bootstrap.pools || []);
  const [snapshotMeta, setSnapshotMeta] = useState<SnapshotMeta>(bootstrap.snapshotMeta || {});
  const [snapshotError, setSnapshotError] = useState<string | null>(null);
  const [realtimeError, setRealtimeError] = useState<string | null>(null);
  const [actionState, setActionState] = useState<string | null>(null);
  const handleRealtimeEvent = useCallback((event: SnapshotEvent) => {
    if (event.event === "action_state_changed") {
      setActionState(event.action_state || event.action_status || "updated");
      return;
    }
    if (!event.sections?.includes("pools")) return;
    if (event.event === "snapshot_refresh_failed") {
      setRealtimeError("Post-check thay đổi Pool thất bại; đang giữ snapshot Pool gần nhất.");
    } else if (event.event === "snapshot_changed") {
      setRealtimeError(null);
      setActionState(null);
    }
  }, []);
  const eventVersion = useClusterSnapshotEvents(bootstrap.clusterId, handleRealtimeEvent);
  const lastGeneration = useRef<number | null>(bootstrap.snapshotMeta?.generation ?? null);
  const initial = bootstrap.selectedPool || (bootstrap.pools.some((row) => row.name === "test") ? "test" : bootstrap.pools[0]?.name || "");
  const [selected, setSelected] = useState(initial);
  const [createOpen, setCreateOpen] = useState(false);
  const [modal, setModal] = useState<"metrics" | "edit" | "scrub" | "details" | "delete" | "protection" | null>(null);
  const [searchOpen, setSearchOpen] = useState(false);
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(1);
  const [columnsOpen, setColumnsOpen] = useState(false);
  const [actionsOpen, setActionsOpen] = useState(false);
  const [visible, setVisible] = useState<Record<string, boolean>>({ redundancy: true, pgs: true, used: true, objects: true, read_iops: true, write_iops: true });
  const selectedRow = useMemo(() => rows.find((row) => row.name === selected), [rows, selected]);
  const snapshotState = getSnapshotState(snapshotMeta, {
    error: bootstrap.queryError || snapshotError || realtimeError,
    hasData: rows.length > 0,
  });
  const filteredRows = useMemo(() => rows.filter((row) => `${row.name} ${row.redundancy} ${row.crush_rule}`.toLowerCase().includes(search.toLowerCase())), [rows, search]);
  const usedBytes = useMemo(() => rows.map((row) => Number(row.used_bytes)).filter((value) => Number.isFinite(value) && value >= 0), [rows]);
  const maxUsedBytes = Math.max(1, ...usedBytes);
  const totalUsedBytes = usedBytes.reduce((total, value) => total + value, 0);
  const totalObjects = useMemo(() => rows.reduce((total, row) => total + (Number(row.objects) || 0), 0), [rows]);
  const totalReadIops = useMemo(() => rows.reduce((total, row) => total + (Number(row.read_iops) || 0), 0), [rows]);
  const totalWriteIops = useMemo(() => rows.reduce((total, row) => total + (Number(row.write_iops) || 0), 0), [rows]);
  const totalPages = Math.max(1, Math.ceil(filteredRows.length / POOLS_PER_PAGE));
  const currentPage = Math.min(page, totalPages);
  const paginatedRows = useMemo(
    () => filteredRows.slice((currentPage - 1) * POOLS_PER_PAGE, currentPage * POOLS_PER_PAGE),
    [filteredRows, currentPage],
  );
  const actionLabels: Record<string, string> = { edit_pool: "cập nhật", scrub_pool: "scrub", delete_pool: "xóa", set_pool_protection: "đổi trạng thái bảo vệ" };
  const openSelected = (value: typeof modal) => { if (selectedRow) setModal(value); };
  const formatBytes = (value: number) => {
    if (!Number.isFinite(value)) return "—";
    const units = ["B", "KiB", "MiB", "GiB", "TiB"];
    let amount = Math.max(0, value);
    let unit = 0;
    while (amount >= 1024 && unit < units.length - 1) { amount /= 1024; unit += 1; }
    return `${amount.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`;
  };

  useEffect(() => {
    let activeController: AbortController | null = null;
    lastGeneration.current = null;
    let requestSequence = 0;
    const onVisibilityChange = () => { if (!document.hidden) load(); };
    const load = () => {
      if (document.hidden) return;
      activeController?.abort();
      const controller = new AbortController();
      activeController = controller;
      const sequence = ++requestSequence;
      fetch(`/api/pools?cluster_id=${encodeURIComponent(bootstrap.clusterId)}`, {
        credentials: "same-origin", signal: controller.signal,
        headers: { "X-Request-ID": `browser-${Date.now()}-${Math.random().toString(36).slice(2, 10)}` },
      })
        .then(async (response) => {
          if (!response.ok) throw new Error(`HTTP ${response.status}`);
          return response.json() as Promise<{ items?: PoolRow[]; meta?: SnapshotMeta }>;
        })
        .then((payload) => {
          if (sequence !== requestSequence) return;
          setSnapshotError(null);
          const generation = payload.meta?.generation ?? null;
          if (payload.meta?.available !== false && Array.isArray(payload.items) && generation !== lastGeneration.current) {
            setRows(payload.items);
            lastGeneration.current = generation;
          }
          if (payload.meta) setSnapshotMeta(payload.meta);
        })
        .catch((error: unknown) => {
          if (error instanceof DOMException && error.name === "AbortError") return;
          setSnapshotError(error instanceof Error ? error.message : "Không thể tải snapshot Pools");
        });
    };
    load();
    const timer = window.setInterval(load, 10_000);
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => { window.clearInterval(timer); document.removeEventListener("visibilitychange", onVisibilityChange); activeController?.abort(); };
  }, [bootstrap.clusterId, eventVersion]);

  useEffect(() => {
    if (bootstrap.actionSuccess !== "set_pool_protection") return;
    // The POST queues work asynchronously. Reload after the Worker's normal
    // polling window so the button reflects Ceph's real nodelete flag instead
    // of the pre-action bootstrap payload returned by the immediate redirect.
    const timer = window.setTimeout(() => {
      const url = new URL(window.location.href);
      url.searchParams.delete("action_success");
      window.location.replace(url.toString());
    }, 6500);
    return () => window.clearTimeout(timer);
  }, [bootstrap.actionSuccess]);

  return (
    <div className="pools-workspace min-h-[620px]">
      <section className="pools-panel overflow-hidden border">
        <div className="border-b border-slate-200 px-5 pt-5">
          <PageHeader title="Pools" />
          <nav className="mt-4 flex gap-7" aria-label="Pool navigation">
            <a href="/pools" className="relative inline-flex items-center gap-2 pb-3 text-sm font-semibold text-violet-700 after:absolute after:inset-x-0 after:bottom-0 after:h-0.5 after:bg-violet-600">
              <Layers3 size={17} /> Pools
            </a>
            <a href="/pgs" className="inline-flex items-center gap-2 pb-3 text-sm font-medium text-slate-500 transition hover:text-violet-700">
              <Grid3X3 size={17} /> PGs
            </a>
          </nav>
        </div>

        {(bootstrap.queryError || snapshotError || realtimeError || snapshotMeta.last_error) && (
          <div className="mx-5 mt-4">
            <ErrorState message={<>Không xác nhận được thay đổi Pool: {realtimeError || bootstrap.queryError || snapshotError || snapshotMeta.last_error}</>} />
          </div>
        )}
        {actionState && <div className="mx-5 mt-3 rounded-md border border-sky-700/50 bg-sky-950/30 px-3 py-2 text-sm text-sky-200" role="status" aria-live="polite">Cập nhật action: <strong>{actionState}</strong>. Đang chờ snapshot Pool mới.</div>}
        <div className="px-5 pt-3 text-xs text-slate-500" role="status" aria-live="polite"><RefreshCw size={13} className="mr-1 inline" />{SNAPSHOT_STATE_LABEL[snapshotState]}{snapshotMeta.collected_at ? ` · generation ${snapshotMeta.generation ?? 0} · ${snapshotMeta.age_seconds == null ? "" : `${Math.round(snapshotMeta.age_seconds)}s ago`}` : ""}</div>
        {bootstrap.createSuccess && <div className="mx-5 mt-4 rounded-md border border-emerald-200 bg-emerald-50 px-4 py-3 text-sm text-emerald-700">Yêu cầu tạo pool đã được gửi tới Worker.</div>}
        {bootstrap.actionSuccess && <div className="mx-5 mt-4 rounded-md border border-emerald-200 bg-emerald-50 px-4 py-3 text-sm text-emerald-700">Yêu cầu {actionLabels[bootstrap.actionSuccess] || bootstrap.actionSuccess} pool đã được gửi tới Worker.</div>}

        <div className="pools-summary-grid" aria-label="Tổng quan Pools">
          <article className="pool-summary-card is-blue"><span className="pool-summary-icon"><Database size={16} /></span><div><strong>{rows.length}</strong><span>Tổng Pools</span></div></article>
          <article className="pool-summary-card is-cyan"><span className="pool-summary-icon"><HardDrive size={16} /></span><div><strong>{usedBytes.length ? formatBytes(totalUsedBytes) : "—"}</strong><span>Dung lượng đã dùng</span></div></article>
          <article className="pool-summary-card is-violet"><span className="pool-summary-icon"><Activity size={16} /></span><div><strong>{totalObjects.toLocaleString()}</strong><span>Tổng Objects</span></div></article>
          <article className="pool-summary-card is-green"><span className="pool-summary-icon"><TrendingUp size={16} /></span><div><strong>{(totalReadIops + totalWriteIops).toLocaleString()}</strong><span>Tổng IOPS</span></div></article>
        </div>

        <div className="pools-toolbar">
          <div className="pools-toolbar-primary">
            {bootstrap.isAdmin && <ToolbarButton icon={Plus} label="Create" onClick={() => setCreateOpen(true)} />}
            <span className="pools-selection-hint">{selectedRow ? `Đã chọn: ${selectedRow.name}` : "Chọn một pool để xem thao tác"}</span>
          </div>
          <div className="pools-toolbar-secondary">
            <div className="relative">
              <ToolbarButton icon={ChevronDown} label="Actions" disabled={!selectedRow} onClick={() => setActionsOpen((value) => !value)} />
              {actionsOpen && selectedRow && <div className="pools-actions-menu" role="menu">
                <button type="button" role="menuitem" onClick={() => { openSelected("metrics"); setActionsOpen(false); }}><TrendingUp size={15} />Metrics</button>
                {bootstrap.isAdmin && <button type="button" role="menuitem" onClick={() => { openSelected("edit"); setActionsOpen(false); }}><Pencil size={15} />Edit</button>}
                {bootstrap.isAdmin && <button type="button" role="menuitem" onClick={() => { openSelected("scrub"); setActionsOpen(false); }}><Brush size={15} />Scrub</button>}
                <button type="button" role="menuitem" onClick={() => { openSelected("details"); setActionsOpen(false); }}><Info size={15} />Details</button>
                {bootstrap.isAdmin && <button type="button" role="menuitem" onClick={() => { openSelected("protection"); setActionsOpen(false); }}><Shield size={15} />{selectedRow.protected ? "Unprotect" : "Protect"}</button>}
                {bootstrap.isAdmin && <button type="button" role="menuitem" className="is-danger" onClick={() => { openSelected("delete"); setActionsOpen(false); }}><X size={15} />Delete</button>}
              </div>}
            </div>
          </div>
          <div className="flex flex-wrap gap-2">
            <ToolbarButton icon={Search} label="Search" onClick={() => setSearchOpen((value) => !value)} />
            <div className="relative"><ToolbarButton icon={ChevronDown} label="Columns" onClick={() => setColumnsOpen((value) => !value)} />
              {columnsOpen && <div className="absolute right-0 z-20 mt-2 w-48 rounded-lg border border-slate-200 bg-white p-3 shadow-xl">{Object.keys(visible).map((key) => <label key={key} className="flex items-center gap-2 py-1 text-sm"><input type="checkbox" checked={visible[key]} onChange={() => setVisible((old) => ({ ...old, [key]: !old[key] }))} />{key.replace("crush_rule", "Crush Rule").replace("read_iops", "Read IOPS").replace("write_iops", "Write IOPS")}</label>)}</div>}
            </div>
          </div>
        </div>
        {searchOpen && <div className="border-b border-slate-200 px-5 py-3"><label className="relative block max-w-md"><Search className="absolute left-3 top-2.5 text-slate-400" size={16} /><input value={search} onChange={(event) => { setSearch(event.target.value); setPage(1); }} autoFocus placeholder="Tìm theo tên hoặc redundancy..." className="h-9 w-full rounded-md border border-slate-200 bg-white pl-9 pr-3 text-sm outline-none focus:border-violet-400" /></label></div>}

        <div className="pools-table-wrap overflow-x-auto px-3 py-4 sm:px-5">
          <table className="pool-table min-w-[920px] w-full text-sm">
            <thead>
              <tr className="text-xs font-semibold uppercase tracking-wide text-slate-500">
                <th className="pool-status-column px-3 py-3 text-left">Status</th>
                <th className="px-3 py-3 text-left">Pool Name</th>
                {visible.redundancy && <th className="px-3 py-3 text-left">Redundancy</th>}
                {visible.pgs && <th className="px-3 py-3 text-right">#PGs</th>}
                {visible.used && <th className="px-3 py-3 text-right">Used disk space</th>}
                {visible.objects && <th className="px-3 py-3 text-right">Objects</th>}
                {visible.read_iops && <th className="px-3 py-3 text-right">Read IOPS</th>}
                {visible.write_iops && <th className="px-3 py-3 text-right">Write IOPS</th>}
              </tr>
            </thead>
            <tbody>
              {filteredRows.length === 0 ? (
                <tr>
                  <td colSpan={8}>
                    {rows.length === 0
                      ? <EmptyState icon={Layers3} message="Chưa có pool." hint="Tạo pool đầu tiên bằng nút Create." />
                      : <EmptyState icon={Search} message="Không tìm thấy pool phù hợp." hint="Thử đổi từ khoá tìm kiếm." />}
                  </td>
                </tr>
              ) : paginatedRows.map((row) => {
                const active = row.name === selected;
                return (
                  <tr
                    key={row.name}
                    onClick={() => setSelected(row.name)}
                    className={`pool-row ${active ? "pool-row-selected" : ""}`}
                  >
                    <td className="pool-status-cell"><span className="pool-status-dot is-active" /><span>Active</span></td>
                    <td className="px-3 py-3 font-semibold text-slate-800">{row.name}</td>
                    {visible.redundancy && <td className="px-3 py-3 text-slate-600">{row.redundancy}</td>}
                    {visible.pgs && <td className="px-3 py-3 text-right tabular-nums">{row.pgs}</td>}
                    {visible.used && <td className="pool-used-cell"><span>{row.used}</span>{Number.isFinite(Number(row.used_bytes)) && <span className="pool-used-meter" role="progressbar" aria-label={`${row.name} used disk space`} aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.min(100, Math.round((Number(row.used_bytes) / maxUsedBytes) * 100))}><i style={{ width: `${Math.max(3, Math.min(100, (Number(row.used_bytes) / maxUsedBytes) * 100))}%` }} /></span>}</td>}
                    {visible.objects && <td className="px-3 py-3 text-right tabular-nums">{row.objects.toLocaleString()}</td>}
                    {visible.read_iops && <td className="px-3 py-3 text-right tabular-nums">{row.read_iops.toLocaleString()}</td>}
                    {visible.write_iops && <td className="px-3 py-3 text-right tabular-nums">{row.write_iops.toLocaleString()}</td>}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>

        <footer className="pools-pagination" aria-label="Phân trang Pools">
          <div className="pools-selected-summary">{selected && <button type="button" onClick={() => setSelected("")} className="btn btn-ghost btn-sm pools-selected-chip">1 selected <X size={14} /></button>}</div>
          <div className="pools-pagination-controls">
            <button type="button" onClick={() => setPage((value) => Math.max(1, value - 1))} disabled={currentPage === 1} aria-label="Trang trước" className="btn btn-ghost btn-sm">← Trang trước</button>
            <span className="pools-pagination-status" role="status" aria-live="polite">Trang {currentPage} / {totalPages}</span>
            <button type="button" onClick={() => setPage((value) => Math.min(totalPages, value + 1))} disabled={currentPage === totalPages} aria-label="Trang sau" className="btn btn-ghost btn-sm">Trang sau →</button>
          </div>
        </footer>
      </section>
      {createOpen && (
        <div className="pools-modal-backdrop fixed inset-0 z-50 grid place-items-center p-4" role="dialog" aria-modal="true" aria-labelledby="create-pool-title" onMouseDown={(event) => { if (event.target === event.currentTarget) setCreateOpen(false); }}>
          <form method="post" action="/pgs/pools/create" className="pools-create-form w-full max-w-lg rounded-xl p-6 shadow-2xl">
            <input type="hidden" name="cluster_id" value={bootstrap.clusterId} />
            <div className="flex items-start justify-between gap-4">
              <div><h2 id="create-pool-title" className="text-xl font-semibold text-slate-900">Create Pool</h2><p className="mt-1 text-sm text-slate-500">Tạo pool mới trên cluster đang chọn.</p></div>
              <button type="button" onClick={() => setCreateOpen(false)} className="grid h-8 w-8 place-items-center rounded-md text-slate-500 hover:bg-slate-100" aria-label="Close"><X size={18} /></button>
            </div>
            <div className="mt-5 grid gap-4">
              <label className="grid gap-2 text-sm font-medium text-slate-700">Pool Name<input name="pool_name" required maxLength={128} pattern="[A-Za-z0-9_.-]+" placeholder="vd: rbd-data" autoFocus className="h-10 rounded-md border border-slate-200 px-3 outline-none focus:border-violet-400" /></label>
              <div className="grid grid-cols-2 gap-4">
                <label className="grid gap-2 text-sm font-medium text-slate-700">Placement Groups<input type="number" name="pg_num" defaultValue={32} min={1} max={32768} required className="h-10 rounded-md border border-slate-200 px-3 outline-none focus:border-violet-400" /></label>
                <label className="grid gap-2 text-sm font-medium text-slate-700">Application<select name="app_name" defaultValue="rbd" className="h-10 rounded-md border border-slate-200 bg-white px-3 outline-none focus:border-violet-400"><option value="rbd">RBD</option><option value="cephfs">CephFS</option><option value="rgw">RGW</option></select></label>
              </div>
            </div>
            <div className="mt-6 flex justify-end gap-2 border-t border-slate-200 pt-4"><button type="button" onClick={() => setCreateOpen(false)} className="h-9 rounded-md border border-slate-200 px-4 text-sm font-medium text-slate-700 hover:bg-slate-50">Cancel</button><button type="submit" className="h-9 rounded-md bg-violet-600 px-4 text-sm font-semibold text-white hover:bg-violet-700">Create</button></div>
          </form>
        </div>
      )}
      {modal && selectedRow && (
        <div className="pools-modal-backdrop fixed inset-0 z-50 grid place-items-center p-4" role="dialog" aria-modal="true" onMouseDown={(event) => { if (event.target === event.currentTarget) setModal(null); }}>
          <form method="post" action="/pools/action" className="pools-create-form w-full max-w-lg rounded-xl p-6 shadow-2xl">
            <input type="hidden" name="cluster_id" value={bootstrap.clusterId} />
            <input type="hidden" name="pool_name" value={selectedRow.name} />
            <input type="hidden" name="action_id" value={modal === "protection" ? "set_pool_protection" : `${modal}_pool`} />
            {modal === "protection" && <input type="hidden" name="protected" value={String(!selectedRow.protected)} />}
            <div className="flex items-start justify-between gap-4">
              <div><h2 className="text-xl font-semibold text-slate-900">{modal[0].toUpperCase() + modal.slice(1)} Pool</h2><p className="mt-1 text-sm text-slate-500">Pool: <strong>{selectedRow.name}</strong></p></div>
              <button type="button" onClick={() => setModal(null)} className="grid h-8 w-8 place-items-center rounded-md text-slate-500 hover:bg-slate-100" aria-label="Close"><X size={18} /></button>
            </div>
            {(modal === "details" || modal === "metrics") && <dl className="mt-5 grid grid-cols-2 gap-3 rounded-lg border border-slate-200 p-4 text-sm">
              <div><dt className="text-slate-500">Redundancy</dt><dd className="font-semibold">{selectedRow.redundancy}</dd></div><div><dt className="text-slate-500">PGs</dt><dd className="font-semibold">{selectedRow.pgs}</dd></div>
              <div><dt className="text-slate-500">Used</dt><dd className="font-semibold">{selectedRow.used}</dd></div><div><dt className="text-slate-500">Objects</dt><dd className="font-semibold">{selectedRow.objects.toLocaleString()}</dd></div>
              <div><dt className="text-slate-500">Read IOPS</dt><dd className="font-semibold">{selectedRow.read_iops.toLocaleString()}</dd></div><div><dt className="text-slate-500">Write IOPS</dt><dd className="font-semibold">{selectedRow.write_iops.toLocaleString()}</dd></div>
              {modal === "details" && <div className="col-span-2"><dt className="text-slate-500">CRUSH Rule</dt><dd className="font-mono font-semibold">{selectedRow.crush_rule}</dd></div>}
            </dl>}
            {modal === "edit" && <div className="mt-5 grid grid-cols-2 gap-4"><label className="grid gap-2 text-sm font-medium">Replicas<input name="size" type="number" min={1} max={10} required defaultValue={selectedRow.size ?? 3} className="h-10 rounded-md border px-3" /></label><label className="grid gap-2 text-sm font-medium">Placement Groups<input name="pg_num" type="number" min={1} max={32768} required defaultValue={selectedRow.pgs} className="h-10 rounded-md border px-3" /></label></div>}
            {modal === "scrub" && <p className="mt-5 text-sm text-slate-600">Worker sẽ yêu cầu Ceph scrub toàn bộ PG thuộc pool này.</p>}
            {modal === "delete" && <p className="mt-5 rounded-md border border-rose-200 bg-rose-50 p-3 text-sm text-rose-700">Xóa pool sẽ xóa vĩnh viễn toàn bộ dữ liệu trong pool.</p>}
            {modal === "protection" && <p className="mt-5 text-sm text-slate-600">{selectedRow.protected ? "Gỡ cờ nodelete để cho phép xóa pool." : "Bật cờ nodelete để ngăn pool bị xóa."}</p>}
            <div className="mt-6 flex justify-end gap-2 border-t border-slate-200 pt-4"><button type="button" onClick={() => setModal(null)} className="h-9 rounded-md border px-4 text-sm">Close</button>{!(["details", "metrics"] as string[]).includes(modal) && <button type="submit" className={`h-9 rounded-md px-4 text-sm font-semibold text-white ${modal === "delete" ? "bg-rose-600" : "bg-violet-600"}`}>Confirm</button>}</div>
          </form>
        </div>
      )}
    </div>
  );
}
