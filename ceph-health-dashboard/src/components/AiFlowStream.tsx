import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Activity, AlertTriangle, BookOpen, Bot, BrainCircuit, CheckCircle2, ClipboardCheck, Database, FlaskConical,
  Gauge, Hand, Play, RefreshCw, ScrollText, Search, Shield, ShieldCheck, Siren,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import "./InstallationStream.css";
import { layoutPositions } from "./CephClusterStream";
import { StreamCanvas } from "./StreamCanvas";
import type { CanvasEdge, CanvasFilter, CanvasGroup, CanvasNode } from "./StreamCanvas";

// "Luồng AI" tab of the Stream page: the whole AI loop (detect → incident →
// evidence → diagnosis → policy → approval/autopilot → execution →
// verification → learning) with the last 24 hours of real numbers per step.
// Read-only; refreshed every 30 s through /api/stream/ai-flow.

type FlowStatus = "ok" | "warn" | "error" | "unknown";
type FlowNode = {
  id: string; group: string; kind: string; title: string; subtitle: string;
  status: FlowStatus; facts: string[]; href?: string | null;
};
export type AiFlow = {
  schema: string;
  generated_at: string;
  window_hours: number;
  summary: { incidents: number; diagnosed: number; evidence: number; pending_approval: number; verified_total: number; problems: number };
  groups: Array<{ id: string; title: string }>;
  nodes: FlowNode[];
  edges: CanvasEdge[];
};

const REFRESH_MS = 30_000;
const STATUS_LABELS: Record<FlowStatus, string> = {
  ok: "Hoạt động bình thường", warn: "Cần chú ý", error: "Lỗi", unknown: "Chưa có dữ liệu",
};
const ICONS: Record<string, LucideIcon> = {
  incident: Siren, evidence: Search, llm: BrainCircuit, policy: ShieldCheck, approval: Hand, autopilot: Bot,
  execute: Play, verify: CheckCircle2, memory: Database, trust: Gauge, online: Activity, lab: FlaskConical,
};

function iconFor(node: FlowNode): LucideIcon {
  if (node.kind === "detect") return node.id === "detect_log" ? ScrollText : node.id === "detect_operator" ? ClipboardCheck : Siren;
  return ICONS[node.kind] || BookOpen;
}

export function AiFlowStream({ initial }: { initial: AiFlow }) {
  const [flow, setFlow] = useState<AiFlow>(initial);
  const [refreshing, setRefreshing] = useState(false);
  const [refreshError, setRefreshError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setRefreshing(true);
    try {
      const response = await fetch("/api/stream/ai-flow", { credentials: "same-origin", headers: { Accept: "application/json" } });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      setFlow(await response.json() as AiFlow);
      setRefreshError(null);
    } catch (error) {
      setRefreshError(error instanceof Error ? error.message : "lỗi không rõ");
    } finally {
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    const timer = window.setInterval(() => { if (!document.hidden) void refresh(); }, REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const groups: CanvasGroup[] = useMemo(() => flow.groups.map((group) => ({
    id: group.id,
    title: group.title,
    nodes: flow.nodes.filter((node) => node.group === group.id).map((node): CanvasNode => ({
      id: node.id, title: node.title, subtitle: node.subtitle, status: node.status,
      icon: iconFor(node), facts: node.facts, configHref: node.href || undefined,
    })),
  })).filter((group) => group.nodes.length), [flow]);
  const layoutKey = flow.nodes.map((node) => `${node.group}:${node.id}`).join("|");
  const layout = useMemo(() => layoutPositions(flow), [layoutKey]);
  const filters: CanvasFilter[] = [
    { key: "all", label: "Tất cả", node: () => true, edge: () => true },
    { key: "problems", label: "Chỉ bước cần chú ý", node: (node) => node.status === "warn" || node.status === "error", edge: () => true },
    { key: "main", label: "Đường chính", node: () => true, edge: (edge) => edge.kind === "data" || edge.kind === "control" },
  ];
  const summary = flow.summary;

  return (
    <div className="installation-stream ceph-cluster-stream">
      <header className="installation-stream__header">
        <div>
          <div className="installation-stream__eyebrow">AI · {flow.window_hours} GIỜ QUA</div>
          <h1>Luồng hoạt động của AI</h1>
          <p>Từ lúc phát hiện sự cố tới chẩn đoán, quyết định, thực thi, xác minh và học lại — kèm số liệu thật từng bước. Trang chỉ đọc.</p>
        </div>
        <div className="installation-stream__header-actions">
          <span className="installation-stream__freshness"><Activity size={14} /> tự làm mới 30 giây</span>
          <button className="installation-stream__refresh" type="button" onClick={() => void refresh()} disabled={refreshing}>
            <RefreshCw size={15} className={refreshing ? "is-spinning" : ""} /> {refreshing ? "Đang làm mới…" : "Làm mới"}
          </button>
        </div>
      </header>
      {refreshError && <p className="ceph-cluster-stream__stale" role="status"><AlertTriangle size={14} /> Không làm mới được ({refreshError}); đang hiển thị dữ liệu lần trước.</p>}

      <section className="installation-stream__summary" aria-label="Tổng quan luồng AI">
        <div className="installation-stream__metric"><span>Incident</span><strong>{summary.incidents}</strong><small>{summary.diagnosed} có chẩn đoán</small></div>
        <div className="installation-stream__metric"><span>Thu bằng chứng</span><strong>{summary.evidence}</strong><small>lần thu theo runbook</small></div>
        <div className="installation-stream__metric"><span>Chờ duyệt</span><strong>{summary.pending_approval}</strong><small>action RISKY đang chờ</small></div>
        <div className="installation-stream__metric"><span>Bước cần chú ý</span><strong className={`ceph-cluster-stream__health ${summary.problems ? "is-warn" : "is-ok"}`}>{summary.problems}</strong><small>{summary.verified_total} case đã xác minh (tổng)</small></div>
      </section>

      <StreamCanvas
        groups={groups}
        edges={flow.edges}
        defaultPositions={layout.positions}
        groupLabelPositions={layout.labels}
        linkLabel="Mở trang"
        detailLinkLabel="Mở trang chi tiết"
        positionKey="ceph-ai:ai-flow-stream:layout:v1"
        statusLabels={STATUS_LABELS}
        filters={filters}
        legend={[
          { className: "is-data", label: "Dữ liệu" },
          { className: "is-control", label: "Quyết định / điều khiển" },
          { className: "is-network", label: "Phản hồi học" },
          { className: "is-management", label: "Kiểm thử (Failure Lab)" },
        ]}
        edgeKinds={["data", "control", "network", "management"]}
        edgeWidth={(kind) => (kind === "data" || kind === "control" ? 2 : 1.5)}
        kicker="AI LOOP"
        title="Phát hiện · chẩn đoán · quyết định · học"
        ariaLabel="Sơ đồ luồng hoạt động của AI"
      />

      <footer className="installation-stream__note"><Shield size={14} /><span>Số liệu 24 giờ từ cơ sở dữ liệu (incident, action, audit, case, trust, online learning) và báo cáo Failure Lab mới nhất. AI chỉ chọn action_id trong danh mục đã duyệt; RISKY luôn cần người duyệt.</span></footer>
    </div>
  );
}
