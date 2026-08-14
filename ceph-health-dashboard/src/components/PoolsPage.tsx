import React, { useMemo, useState } from "react";
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
};

type PoolsBootstrap = {
  pools: PoolRow[];
  selectedPool?: string | null;
  updatedAt: string;
  updatedAgo: string;
  isAdmin: boolean;
  clusterId: string;
  createSuccess?: boolean;
  queryError?: string | null;
};

const samplePools: PoolRow[] = [
  { name: ".mgr", redundancy: "2/3 Replicated", pgs: 1, crush_rule: "replicated_rule", used: "925.7 kB", objects: 2, read_iops: 0, write_iops: 0 },
  { name: "images", redundancy: "2/3 Replicated", pgs: 16, crush_rule: "replicated_rule", used: "0 B", objects: 0, read_iops: 0, write_iops: 0 },
  { name: "volumes", redundancy: "2/3 Replicated", pgs: 16, crush_rule: "replicated_rule", used: "4.67 GB", objects: 776, read_iops: 0, write_iops: 0 },
  { name: "vms", redundancy: "2/3 Replicated", pgs: 16, crush_rule: "replicated_rule", used: "0 B", objects: 0, read_iops: 0, write_iops: 0 },
  { name: "test", redundancy: "2/3 Replicated", pgs: 32, crush_rule: "replicated_rule", used: "0 B", objects: 0, read_iops: 0, write_iops: 0 },
  { name: "images2", redundancy: "2/3 Replicated", pgs: 64, crush_rule: "replicated_rule", used: "26.85 GB", objects: 3375, read_iops: 0, write_iops: 0 },
];

function ToolbarButton({ icon: Icon, label, danger = false, onClick }: { icon: React.ElementType; label: string; danger?: boolean; onClick?: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={danger
        ? "inline-flex h-9 items-center gap-2 rounded-md border border-rose-200 bg-rose-50 px-3 text-sm font-medium text-rose-600 transition hover:border-rose-300 hover:bg-rose-100"
        : "inline-flex h-9 items-center gap-2 rounded-md border border-slate-200 bg-white px-3 text-sm font-medium text-slate-700 transition hover:border-violet-300 hover:bg-violet-50 hover:text-violet-700"}
    >
      <Icon size={16} strokeWidth={1.8} aria-hidden="true" />
      {label}
    </button>
  );
}

export function PoolsPage({ bootstrap }: { bootstrap: PoolsBootstrap }) {
  const rows = bootstrap.pools.length ? bootstrap.pools : samplePools;
  const initial = bootstrap.selectedPool || (rows.some((row) => row.name === "test") ? "test" : rows[0]?.name || "");
  const [selected, setSelected] = useState(initial);
  const [createOpen, setCreateOpen] = useState(false);
  const selectedRow = useMemo(() => rows.find((row) => row.name === selected), [rows, selected]);

  return (
    <div className="pools-workspace min-h-[620px] rounded-xl p-4 font-sans sm:p-6">
      <section className="pools-panel overflow-hidden rounded-lg border shadow-[0_4px_18px_rgba(15,23,42,0.08)]">
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

        <div className="flex flex-col gap-3 border-b border-slate-200 px-5 py-4 xl:flex-row xl:items-center xl:justify-between">
          <div className="flex flex-wrap gap-2">
            <ToolbarButton icon={TrendingUp} label="Metrics" />
            {bootstrap.isAdmin && <ToolbarButton icon={Plus} label="Create" onClick={() => setCreateOpen(true)} />}
            <ToolbarButton icon={Pencil} label="Edit" />
            <ToolbarButton icon={Brush} label="Scrub" />
            <ToolbarButton icon={Info} label="Details" />
            <ToolbarButton icon={X} label="Delete" danger />
          </div>
          <div className="flex flex-wrap gap-2">
            <ToolbarButton icon={Search} label="Search" />
            <ToolbarButton icon={ChevronDown} label="Columns" />
          </div>
        </div>

        <div className="overflow-x-auto px-3 pt-2 sm:px-5">
          <table className="min-w-[1000px] w-full border-separate border-spacing-y-1 text-sm">
            <thead>
              <tr className="text-xs font-semibold uppercase tracking-wide text-slate-500">
                <th className="w-5 px-1 py-3"><span className="sr-only">Health</span></th>
                <th className="px-3 py-3 text-left">Pool Name</th>
                <th className="px-3 py-3 text-left">Redundancy</th>
                <th className="px-3 py-3 text-right">#PGs</th>
                <th className="px-3 py-3 text-left">Crush Rule</th>
                <th className="px-3 py-3 text-right">Used disk space</th>
                <th className="px-3 py-3 text-right">Objects</th>
                <th className="px-3 py-3 text-right">Read IOPS</th>
                <th className="px-3 py-3 text-right">Write IOPS</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row, index) => {
                const active = row.name === selected;
                return (
                  <tr
                    key={row.name}
                    onClick={() => setSelected(row.name)}
                    className={`${active ? "bg-blue-100" : index % 2 ? "bg-amber-50/70" : "bg-white"} cursor-pointer transition hover:bg-violet-50 focus-within:bg-violet-50`}
                  >
                    <td className="rounded-l-md px-1 py-3"><span className="block h-2.5 w-2.5 rounded-full bg-orange-400 ring-2 ring-orange-100" /></td>
                    <td className="px-3 py-3 font-semibold text-slate-800">{row.name}</td>
                    <td className="px-3 py-3 text-slate-600">{row.redundancy}</td>
                    <td className="px-3 py-3 text-right tabular-nums">{row.pgs}</td>
                    <td className="px-3 py-3"><span className="rounded-full bg-slate-100 px-2.5 py-1 font-mono text-xs text-slate-600">{row.crush_rule}</span></td>
                    <td className="px-3 py-3 text-right tabular-nums">{row.used}</td>
                    <td className="px-3 py-3 text-right tabular-nums">{row.objects.toLocaleString()}</td>
                    <td className="px-3 py-3 text-right tabular-nums">{row.read_iops.toLocaleString()}</td>
                    <td className="rounded-r-md px-3 py-3 text-right tabular-nums">{row.write_iops.toLocaleString()}</td>
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
    </div>
  );
}
