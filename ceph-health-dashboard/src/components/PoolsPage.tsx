import React, { useEffect, useMemo, useState } from "react";
import {
  Brush,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Grid3X3,
  Info,
  Layers3,
  Pencil,
  Plus,
  Search,
  Shield,
  ShieldOff,
  TrendingUp,
  X,
} from "lucide-react";

type PoolRow = {
  name: string;
  redundancy: string;
  pgs: number;
  crush_rule: string;
  used: string;
  objects: number;
  read_iops: number;
  write_iops: number;
  size: number | null;
  protected: boolean;
};

type PoolsBootstrap = {
  pools: PoolRow[];
  selectedPool?: string | null;
  updatedAt: string;
  updatedAgo: string;
  isAdmin: boolean;
  clusterId: string;
  createSuccess?: boolean;
  actionSuccess?: string | null;
  queryError?: string | null;
};

function ToolbarButton({ icon: Icon, label, danger = false, disabled = false, onClick }: { icon: React.ElementType; label: string; danger?: boolean; disabled?: boolean; onClick?: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className={danger
        ? "inline-flex h-9 items-center gap-2 rounded-md border border-rose-200 bg-rose-50 px-3 text-sm font-medium text-rose-600 transition hover:border-rose-300 hover:bg-rose-100"
        : "inline-flex h-9 items-center gap-2 rounded-md border border-slate-200 bg-white px-3 text-sm font-medium text-slate-700 transition hover:border-violet-300 hover:bg-violet-50 hover:text-violet-700 disabled:cursor-not-allowed disabled:opacity-40"}
    >
      <Icon size={16} strokeWidth={1.8} aria-hidden="true" />
      {label}
    </button>
  );
}

export function PoolsPage({ bootstrap }: { bootstrap: PoolsBootstrap }) {
  const rows = bootstrap.pools;
  const initial = bootstrap.selectedPool || (rows.some((row) => row.name === "test") ? "test" : rows[0]?.name || "");
  const [selected, setSelected] = useState(initial);
  const [createOpen, setCreateOpen] = useState(false);
  const [modal, setModal] = useState<"metrics" | "edit" | "scrub" | "details" | "delete" | "protection" | null>(null);
  const [searchOpen, setSearchOpen] = useState(false);
  const [search, setSearch] = useState("");
  const [columnsOpen, setColumnsOpen] = useState(false);
  const [visible, setVisible] = useState<Record<string, boolean>>({ redundancy: true, pgs: true, crush_rule: true, used: true, objects: true, read_iops: true, write_iops: true });
  const selectedRow = useMemo(() => rows.find((row) => row.name === selected), [rows, selected]);
  const filteredRows = useMemo(() => rows.filter((row) => `${row.name} ${row.redundancy} ${row.crush_rule}`.toLowerCase().includes(search.toLowerCase())), [rows, search]);
  const actionLabels: Record<string, string> = { edit_pool: "cập nhật", scrub_pool: "scrub", delete_pool: "xóa", set_pool_protection: "đổi trạng thái bảo vệ" };
  const openSelected = (value: typeof modal) => { if (selectedRow) setModal(value); };

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
    <div className="pools-workspace min-h-[620px] font-sans">
      <section className="pools-panel overflow-hidden border">
        <header className="border-b border-slate-200 px-5 pt-5">
          <div className="flex items-center gap-2 pb-5">
            <h1 className="text-2xl font-semibold tracking-tight text-slate-900">Pools</h1>
            {selectedRow && <><span className="text-xl text-slate-300">›</span><span className="text-lg font-normal text-slate-500">{selectedRow.name}</span></>}
          </div>
          <nav className="flex gap-7" aria-label="Pool navigation">
            <a href="/pools" className="relative inline-flex items-center gap-2 pb-3 text-sm font-semibold text-violet-700 after:absolute after:inset-x-0 after:bottom-0 after:h-0.5 after:bg-violet-600">
              <Layers3 size={17} /> Pools
            </a>
            <a href="/pgs" className="inline-flex items-center gap-2 pb-3 text-sm font-medium text-slate-500 transition hover:text-violet-700">
              <Grid3X3 size={17} /> PGs
            </a>
          </nav>
        </header>

        {bootstrap.queryError && <div className="mx-5 mt-4 rounded-md border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">Không lấy được dữ liệu Pool: {bootstrap.queryError}</div>}
        {bootstrap.createSuccess && <div className="mx-5 mt-4 rounded-md border border-emerald-200 bg-emerald-50 px-4 py-3 text-sm text-emerald-700">Yêu cầu tạo pool đã được gửi tới Worker.</div>}
        {bootstrap.actionSuccess && <div className="mx-5 mt-4 rounded-md border border-emerald-200 bg-emerald-50 px-4 py-3 text-sm text-emerald-700">Yêu cầu {actionLabels[bootstrap.actionSuccess] || bootstrap.actionSuccess} pool đã được gửi tới Worker.</div>}

        <div className="flex flex-col gap-3 border-b border-slate-200 px-5 py-4 xl:flex-row xl:items-center xl:justify-between">
          <div className="flex flex-wrap gap-2">
            <ToolbarButton icon={TrendingUp} label="Metrics" disabled={!selectedRow} onClick={() => openSelected("metrics")} />
            {bootstrap.isAdmin && <ToolbarButton icon={Plus} label="Create" onClick={() => setCreateOpen(true)} />}
            {bootstrap.isAdmin && <ToolbarButton icon={Pencil} label="Edit" disabled={!selectedRow} onClick={() => openSelected("edit")} />}
            {bootstrap.isAdmin && <ToolbarButton icon={Brush} label="Scrub" disabled={!selectedRow} onClick={() => openSelected("scrub")} />}
            <ToolbarButton icon={Info} label="Details" disabled={!selectedRow} onClick={() => openSelected("details")} />
            {bootstrap.isAdmin && <ToolbarButton icon={X} label="Delete" danger disabled={!selectedRow} onClick={() => openSelected("delete")} />}
            {bootstrap.isAdmin && selectedRow && <ToolbarButton icon={selectedRow.protected ? ShieldOff : Shield} label={selectedRow.protected ? "Unprotect" : "Protect"} onClick={() => openSelected("protection")} />}
          </div>
          <div className="flex flex-wrap gap-2">
            <ToolbarButton icon={Search} label="Search" onClick={() => setSearchOpen((value) => !value)} />
            <div className="relative"><ToolbarButton icon={ChevronDown} label="Columns" onClick={() => setColumnsOpen((value) => !value)} />
              {columnsOpen && <div className="absolute right-0 z-20 mt-2 w-48 rounded-lg border border-slate-200 bg-white p-3 shadow-xl">{Object.keys(visible).map((key) => <label key={key} className="flex items-center gap-2 py-1 text-sm"><input type="checkbox" checked={visible[key]} onChange={() => setVisible((old) => ({ ...old, [key]: !old[key] }))} />{key.replace("crush_rule", "Crush Rule").replace("read_iops", "Read IOPS").replace("write_iops", "Write IOPS")}</label>)}</div>}
            </div>
          </div>
        </div>
        {searchOpen && <div className="border-b border-slate-200 px-5 py-3"><label className="relative block max-w-md"><Search className="absolute left-3 top-2.5 text-slate-400" size={16} /><input value={search} onChange={(event) => setSearch(event.target.value)} autoFocus placeholder="Tìm theo tên, redundancy hoặc CRUSH rule..." className="h-9 w-full rounded-md border border-slate-200 bg-white pl-9 pr-3 text-sm outline-none focus:border-violet-400" /></label></div>}

        <div className="pools-table-wrap overflow-x-auto px-3 py-4 sm:px-5">
          <table className="min-w-[1000px] w-full text-sm">
            <thead>
              <tr className="text-xs font-semibold uppercase tracking-wide text-slate-500">
                <th className="w-5 px-1 py-3"><span className="sr-only">Health</span></th>
                <th className="px-3 py-3 text-left">Pool Name</th>
                {visible.redundancy && <th className="px-3 py-3 text-left">Redundancy</th>}
                {visible.pgs && <th className="px-3 py-3 text-right">#PGs</th>}
                {visible.crush_rule && <th className="px-3 py-3 text-left">Crush Rule</th>}
                {visible.used && <th className="px-3 py-3 text-right">Used disk space</th>}
                {visible.objects && <th className="px-3 py-3 text-right">Objects</th>}
                {visible.read_iops && <th className="px-3 py-3 text-right">Read IOPS</th>}
                {visible.write_iops && <th className="px-3 py-3 text-right">Write IOPS</th>}
              </tr>
            </thead>
            <tbody>
              {filteredRows.length === 0 ? (
                <tr>
                  <td colSpan={9} className="px-3 py-12 text-center text-slate-500">
                    {rows.length === 0 ? "Chưa có pool." : "Không tìm thấy pool phù hợp."}
                  </td>
                </tr>
              ) : filteredRows.map((row, index) => {
                const active = row.name === selected;
                return (
                  <tr
                    key={row.name}
                    onClick={() => setSelected(row.name)}
                    className={`${active ? "bg-blue-100" : index % 2 ? "bg-amber-50/70" : "bg-white"} cursor-pointer transition hover:bg-violet-50 focus-within:bg-violet-50`}
                  >
                    <td className="px-3 py-3"><span className="block h-2.5 w-2.5 rounded-full bg-orange-400 ring-2 ring-orange-100" /></td>
                    <td className="px-3 py-3 font-semibold text-slate-800">{row.name}</td>
                    {visible.redundancy && <td className="px-3 py-3 text-slate-600">{row.redundancy}</td>}
                    {visible.pgs && <td className="px-3 py-3 text-right tabular-nums">{row.pgs}</td>}
                    {visible.crush_rule && <td className="px-3 py-3"><span className="rounded-full bg-slate-100 px-2.5 py-1 font-mono text-xs text-slate-600">{row.crush_rule}</span></td>}
                    {visible.used && <td className="px-3 py-3 text-right tabular-nums">{row.used}</td>}
                    {visible.objects && <td className="px-3 py-3 text-right tabular-nums">{row.objects.toLocaleString()}</td>}
                    {visible.read_iops && <td className="px-3 py-3 text-right tabular-nums">{row.read_iops.toLocaleString()}</td>}
                    {visible.write_iops && <td className="px-3 py-3 text-right tabular-nums">{row.write_iops.toLocaleString()}</td>}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>

        <footer className="flex flex-col gap-4 border-t border-slate-200 px-5 py-4 text-sm lg:flex-row lg:items-center lg:justify-between">
          <div>{selected && <button type="button" onClick={() => setSelected("")} className="inline-flex items-center gap-2 rounded-full bg-slate-100 px-3 py-1.5 text-slate-600 transition hover:bg-slate-200">1 selected <X size={14} /></button>}</div>
          <div className="flex flex-wrap items-center justify-center gap-3 text-slate-600">
            <span>Page</span><input value="1" readOnly aria-label="Page number" className="h-8 w-10 rounded border border-slate-200 bg-white text-center outline-none focus:border-violet-400" /><span>of 1</span>
            <label className="ml-2 inline-flex items-center gap-2">Rows per page:<select defaultValue="20" className="h-8 rounded border border-slate-200 bg-white px-2 outline-none focus:border-violet-400"><option>20</option></select></label>
            <button disabled className="grid h-8 w-8 place-items-center rounded border border-slate-200 text-slate-300"><ChevronLeft size={16} /></button>
            <button disabled className="grid h-8 w-8 place-items-center rounded border border-slate-200 text-slate-300"><ChevronRight size={16} /></button>
          </div>
          <div className="text-right text-xs text-slate-400"><div>{bootstrap.updatedAgo}</div><time>{bootstrap.updatedAt}</time></div>
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
