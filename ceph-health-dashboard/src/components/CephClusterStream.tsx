import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Activity, AlertTriangle, Bot, Cable, Cloud, Crown, Database, HardDrive, Layers, Network, RefreshCw, Server, Shield, Users,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import "./InstallationStream.css";
import { StreamCanvas } from "./StreamCanvas";
import type { CanvasEdge, CanvasFilter, CanvasGroup, CanvasNode, Point } from "./StreamCanvas";

// Ceph cluster view of the Stream page (Plan/in-progress/ceph-cluster-stream-
// plan-2026-10-06.md): services, hosts (OSDs grouped by host) and the Ceph
// networks with live status from the stored snapshots. Read-only; refreshed
// every 30 s through /api/stream/ceph-topology while the tab is visible.

type TopologyStatus = "ok" | "warn" | "error" | "unknown";
type TopologyCheck = { code: string; severity?: string; message: string };
type TopologyNode = {
  id: string;
  group: string;
  kind: string;
  title: string;
  subtitle: string;
  status: TopologyStatus;
  facts: string[];
  checks: TopologyCheck[];
  href?: string | null;
};
export type CephTopology = {
  schema: string;
  cluster: { id: string; name: string };
  generated_at: string;
  stale: boolean;
  snapshots: Record<string, string | null | undefined>;
  summary: {
    health?: string | null;
    mons_in_quorum: number;
    mons: number;
    osds_up: number;
    osds_in: number;
    osds: number;
    pgs_active_clean: number;
    pgs: number;
    used_percent: number | null;
    public_network: string[];
    cluster_network: string[];
  };
  groups: Array<{ id: string; title: string }>;
  nodes: TopologyNode[];
  edges: CanvasEdge[];
};

const REFRESH_MS = 30_000;
const STATUS_LABELS: Record<TopologyStatus, string> = {
  ok: "Hoạt động bình thường",
  warn: "Có cảnh báo",
  error: "Lỗi",
  unknown: "Chưa rõ (snapshot cũ hoặc thiếu)",
};
const ICONS: Record<string, LucideIcon> = {
  client: Users, mon: Crown, mgr: Activity, mds: Layers, rgw: Cloud, osd: HardDrive,
  pools: Database, host: Server, network: Network,
};

function iconFor(node: TopologyNode): LucideIcon {
  if (node.id === "ceph_ai") return Bot;
  if (node.id === "net_management") return Cable;
  return ICONS[node.kind] || Shield;
}

// Left-to-right flow: one column per group (client → control plane →
// gateway/data → hosts → networks), nodes stacked in their column.
type ColumnGraph = { groups: Array<{ id: string }>; nodes: Array<{ id: string; group: string }> };

export function layoutPositions(topology: ColumnGraph): { positions: Record<string, Point>; labels: Record<string, Point> } {
  const positions: Record<string, Point> = {};
  const labels: Record<string, Point> = {};
  let column = 0;
  for (const group of topology.groups) {
    const nodes = topology.nodes.filter((node) => node.group === group.id);
    if (!nodes.length) continue;
    const x = 35 + column * 300;
    labels[group.id] = { x: x - 15, y: 20 };
    nodes.forEach((node, index) => { positions[node.id] = { x, y: 65 + index * 120 }; });
    column += 1;
  }
  return { positions, labels };
}

function formatTimestamp(value: string | null | undefined): string {
  if (!value) return "chưa có";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "chưa rõ";
  return new Intl.DateTimeFormat("vi-VN", {
    day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  }).format(date).replace(",", "");
}

function healthClass(health: string | null | undefined, stale: boolean): string {
  if (stale) return "is-unknown";
  if (health === "HEALTH_OK") return "is-ok";
  if (health === "HEALTH_ERR") return "is-error";
  return health ? "is-warn" : "is-unknown";
}

export function CephClusterStream({ initial }: { initial: CephTopology }) {
  const [topology, setTopology] = useState<CephTopology>(initial);
  const [refreshing, setRefreshing] = useState(false);
  const [refreshError, setRefreshError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setRefreshing(true);
    try {
      const response = await fetch(`/api/stream/ceph-topology${window.location.search}`, {
        credentials: "same-origin", headers: { Accept: "application/json" },
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      setTopology(await response.json() as CephTopology);
      setRefreshError(null);
    } catch (error) {
      setRefreshError(error instanceof Error ? error.message : "lỗi không rõ");
    } finally {
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    const timer = window.setInterval(() => { if (!document.hidden) void refresh(); }, REFRESH_MS);
    const onVisible = () => { if (!document.hidden) void refresh(); };
    document.addEventListener("visibilitychange", onVisible);
    return () => { window.clearInterval(timer); document.removeEventListener("visibilitychange", onVisible); };
  }, [refresh]);

  const byId = useMemo(() => new Map(topology.nodes.map((node) => [node.id, node])), [topology]);
  const groups: CanvasGroup[] = useMemo(() => topology.groups.map((group) => ({
    id: group.id,
    title: group.title,
    nodes: topology.nodes.filter((node) => node.group === group.id).map((node): CanvasNode => ({
      id: node.id, title: node.title, subtitle: node.subtitle, status: node.status,
      icon: iconFor(node), facts: node.facts, configHref: node.href || undefined,
    })),
  })).filter((group) => group.nodes.length), [topology]);
  // Positions depend on which nodes exist, not on their status, so a
  // 30-second refresh keeps the layout still.
  const layoutKey = topology.nodes.map((node) => `${node.group}:${node.id}`).join("|");
  const layout = useMemo(() => layoutPositions(topology), [layoutKey]);
  const problems = topology.nodes.filter((node) => node.status === "warn" || node.status === "error");
  const filters: CanvasFilter[] = [
    { key: "all", label: "Tất cả", node: () => true, edge: () => true },
    { key: "problems", label: "Chỉ thành phần có vấn đề", node: (node) => node.status !== "ok", edge: () => true },
    { key: "data", label: "Đường dữ liệu", node: (node) => !["net_management", "ceph_ai"].includes(node.id), edge: (edge) => edge.kind === "data" || edge.kind === "network" },
  ];
  const summary = topology.summary;
  const renderChecks = (node: CanvasNode) => {
    const source = byId.get(node.id);
    if (!source) return null;
    return (
      <section className="installation-stream__traceability" aria-label="Health check liên quan">
        <h3>Health check liên quan</h3>
        {source.checks.length
          ? <ul>{source.checks.map((check) => <li key={check.code}><code>{check.code}</code> {check.message}</li>)}</ul>
          : <p className="installation-stream__mapping-missing">Không có health check nào (bỏ qua check đã mute).</p>}
      </section>
    );
  };

  return (
    <div className="installation-stream ceph-cluster-stream">
      <header className="installation-stream__header">
        <div>
          <div className="installation-stream__eyebrow">CEPH CLUSTER · {topology.cluster.name}</div>
          <h1>Luồng dịch vụ cụm Ceph</h1>
          <p>Dịch vụ, host và đường mạng của cụm đang chọn, kèm trạng thái từ snapshot watcher đã thu. Trang chỉ đọc, không chạy lệnh Ceph khi mở.</p>
        </div>
        <div className="installation-stream__header-actions">
          <span className="installation-stream__freshness"><Activity size={14} /> Health {formatTimestamp(topology.snapshots.health)} · tự làm mới 30 giây</span>
          <button className="installation-stream__refresh" type="button" onClick={() => void refresh()} disabled={refreshing}>
            <RefreshCw size={15} className={refreshing ? "is-spinning" : ""} /> {refreshing ? "Đang làm mới…" : "Làm mới"}
          </button>
        </div>
      </header>
      {topology.stale && <p className="ceph-cluster-stream__stale" role="status"><AlertTriangle size={14} /> Snapshot đã cũ hoặc thiếu: mọi trạng thái hiển thị là "chưa rõ" cho tới khi watcher thu lại.</p>}
      {refreshError && <p className="ceph-cluster-stream__stale" role="status"><AlertTriangle size={14} /> Không làm mới được ({refreshError}); đang hiển thị dữ liệu lần trước.</p>}

      <section className="installation-stream__summary" aria-label="Tổng quan cụm Ceph">
        <div className="installation-stream__metric"><span>Health</span><strong className={`ceph-cluster-stream__health ${healthClass(summary.health, topology.stale)}`}>{summary.health || "chưa rõ"}</strong><small>{problems.length ? `${problems.length} thành phần có vấn đề` : "không có thành phần lỗi"}</small></div>
        <div className="installation-stream__metric"><span>MON quorum</span><strong>{summary.mons_in_quorum}/{summary.mons}</strong><small>OSD {summary.osds_up}/{summary.osds} up · {summary.osds_in} in</small></div>
        <div className="installation-stream__metric"><span>PG active+clean</span><strong>{summary.pgs_active_clean}<small> / {summary.pgs}</small></strong><small>dung lượng đã dùng {summary.used_percent ?? "—"}%</small></div>
        <div className="installation-stream__metric"><span>Mạng Ceph</span><strong>{summary.public_network.join(", ") || "chưa rõ"}</strong><small>{summary.cluster_network.length ? `cluster: ${summary.cluster_network.join(", ")}` : "không có cluster network riêng"}</small></div>
      </section>

      <StreamCanvas
        groups={groups}
        edges={topology.edges}
        defaultPositions={layout.positions}
        groupLabelPositions={layout.labels}
        linkLabel="Mở trang"
        detailLinkLabel="Mở trang chi tiết"
        positionKey={`ceph-ai:ceph-cluster-stream:layout:v2:${topology.cluster.id}`}
        statusLabels={STATUS_LABELS}
        filters={filters}
        legend={[
          { className: "is-data", label: "Dữ liệu" },
          { className: "is-control", label: "Điều khiển / cluster map" },
          { className: "is-network", label: "Gắn vào mạng" },
          { className: "is-management", label: "Quản trị (SSH)" },
        ]}
        edgeKinds={["data", "control", "network", "management"]}
        edgeWidth={(kind) => (kind === "data" ? 2 : kind === "management" ? 1 : 1.5)}
        kicker="CEPH SERVICES"
        title="Dịch vụ · host · mạng"
        ariaLabel="Sơ đồ dịch vụ cụm Ceph"
        renderDetailExtra={renderChecks}
      />

      <footer className="installation-stream__note"><Shield size={14} /><span>Trạng thái lấy từ snapshot watcher (health, ceph -s, inventory); OSD gom theo host theo cây CRUSH; mạng theo cấu hình public_network/cluster_network.</span></footer>
    </div>
  );
}
