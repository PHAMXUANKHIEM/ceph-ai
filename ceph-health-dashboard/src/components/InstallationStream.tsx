import { useMemo, useState } from "react";
import {
  Activity, Bot, Boxes, Check, Cloud, Database, GitBranch, HardDrive, RefreshCw, Server, Shield, Waypoints, Workflow,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import "./InstallationStream.css";
import { StreamCanvas } from "./StreamCanvas";
import type { CanvasEdge, CanvasFilter, CanvasNode, Point } from "./StreamCanvas";

type FeatureState = { status: "configured" | "disabled" | "not_configured"; [key: string]: unknown };
type ClusterProfile = {
  label: string;
  active: boolean;
  default: boolean;
  execution_mode: string;
  node_counts: Record<string, number>;
  backup: boolean;
};
type ArchitectureLink = {
  architecture_ids: string[];
  flows: Array<{ id: string; criticality: string }>;
  tests: string[];
  relationships: Array<{ from: string; kind: string; to: string }>;
};
export type InstallationProfile = {
  generated_at: string;
  secrets_included: false;
  ceph_execution_modes: string[];
  clusters: ClusterProfile[];
  vitastor: { active_clusters: number };
  features: Record<string, FeatureState>;
  architecture?: Record<string, ArchitectureLink>;
};

type NodeStatus = FeatureState["status"] | "core";
type GraphNode = CanvasNode & { status: NodeStatus; architecture?: ArchitectureLink };
type GraphEdge = CanvasEdge & { kind: "core" | "optional" | "unconfigured" };
type GraphGroup = { id: string; title: string; nodes: GraphNode[] };

const DEFAULT_POSITIONS: Record<string, Point> = {
  browser: { x: 35, y: 65 }, dashboard: { x: 305, y: 65 }, database: { x: 575, y: 65 },
  watcher: { x: 35, y: 295 }, rabbit: { x: 305, y: 295 }, worker: { x: 575, y: 295 }, ceph: { x: 845, y: 295 },
  object_storage: { x: 25, y: 535 }, openstack: { x: 245, y: 535 }, ai_provider: { x: 465, y: 535 },
  backup: { x: 685, y: 535 }, telegram: { x: 905, y: 535 }, log_intelligence: { x: 25, y: 645 },
  vitastor: { x: 245, y: 645 }, federated_identity: { x: 465, y: 645 }, vault_monitoring: { x: 685, y: 645 },
};
const POSITION_KEY = "ceph-ai:installation-stream:layout:v2";

const featureSpecs: Array<{ key: string; title: string; subtitle: string; icon: LucideIcon; configHref: string }> = [
  { key: "object_storage", title: "RGW / S3", subtitle: "Object Storage", icon: Boxes, configHref: "/settings#cluster" },
  { key: "openstack", title: "OpenStack", subtitle: "Cinder integration", icon: Cloud, configHref: "/settings#openstack" },
  { key: "ai_provider", title: "AI Provider", subtitle: "Chat / diagnosis", icon: Bot, configHref: "/settings#router" },
  { key: "backup", title: "Backup target", subtitle: "SSH / S3", icon: HardDrive, configHref: "/settings#backup-targets" },
  { key: "telegram", title: "Telegram", subtitle: "Alerts / approval", icon: Activity, configHref: "/telegram-alerts" },
  { key: "log_intelligence", title: "Log source", subtitle: "SSH / Loki / ES", icon: GitBranch, configHref: "/settings#log-intel" },
  { key: "vitastor", title: "Vitastor", subtitle: "Separate product", icon: Database, configHref: "/vitastor" },
  { key: "federated_identity", title: "Federated IAM", subtitle: "OIDC / LDAP / AD", icon: Shield, configHref: "/federated-iam" },
  { key: "vault_monitoring", title: "Vault", subtitle: "Security monitoring", icon: Shield, configHref: "/settings#cluster" },
];

const statusLabels: Record<NodeStatus, string> = {
  core: "Luồng lõi",
  configured: "Đã cấu hình · có điều kiện",
  disabled: "Chưa bật hoặc chưa cấu hình",
  not_configured: "Chưa bật hoặc chưa cấu hình",
};

function detailsForFeature(key: string, feature: FeatureState): string[] {
  const facts: string[] = [];
  if (key === "telegram") {
    facts.push(`Kênh đã cấu hình: ${Array.isArray(feature.configured_channels) ? feature.configured_channels.join(", ") || "không có" : "không có"}`);
    facts.push(`Listener Telegram: ${feature.listener_enabled ? "bật" : "tắt"}`);
  } else if (key === "log_intelligence") {
    facts.push(`Nguồn log: ${String(feature.source || "chưa xác định")}`);
    facts.push(`Collector: ${feature.enabled ? "bật" : "tắt"}`);
  } else if (key === "vitastor") {
    facts.push(`Cluster đang hoạt động: ${String(feature.active_clusters ?? 0)}`);
  } else if (key === "federated_identity") {
    const providers = feature.enabled_provider_types;
    facts.push(`Provider đã bật: ${providers && typeof providers === "object" ? Object.entries(providers as Record<string, number>).map(([name, count]) => `${name} (${count})`).join(", ") || "không có" : "không có"}`);
    facts.push(`Worker role mapping: ${Array.isArray(feature.worker_role_mapping_support) ? feature.worker_role_mapping_support.join(", ") : "không có"}`);
  }
  if (!facts.length) facts.push(feature.status === "configured" ? "Integration được nhận diện từ cấu hình đã lưu." : "Integration tùy chọn; hiện chưa phát hiện cấu hình đang hoạt động.");
  return facts;
}

function makeGraph(profile: InstallationProfile): { groups: GraphGroup[]; edges: GraphEdge[] } {
  const activeCeph = profile.clusters.filter((cluster) => cluster.active);
  const core: GraphNode[] = [
    { id: "browser", title: "Operator UI", subtitle: "Jinja · React", status: "core", icon: Workflow, facts: ["Dashboard trên trình duyệt", "Signed session và cluster context"] },
    { id: "dashboard", title: "Dashboard API", subtitle: "FastAPI · RBAC · scope", status: "core", icon: Server, facts: ["Xác thực và phân quyền", "Route đọc snapshot / cache / DB", "Một số tích hợp tùy cấu hình"] },
    { id: "database", title: "SQL Database", subtitle: "State · snapshots · audit", status: "core", icon: Database, facts: ["Cấu hình cluster và integration", "Incident / Action / audit", "Snapshot và metrics đã lưu"] },
  ];
  const processing: GraphNode[] = [
    { id: "watcher", title: "Watcher", subtitle: "Collectors · scheduled loops", status: "core", icon: Activity, facts: ["Thu thập health và telemetry", `${activeCeph.length} Ceph cluster đang hoạt động`, "Collector chạy độc lập theo feature"] },
    { id: "rabbit", title: "RabbitMQ", subtitle: "Incident / task queues", status: "core", icon: Waypoints, facts: ["Incident queue và dead-letter", "Delegated AI dùng queue riêng"] },
    { id: "worker", title: "Worker", subtitle: "Actions · jobs · outboxes", status: "core", icon: Boxes, facts: ["Incident và delegated-AI consumers", "Approval-gated actions", "Backup, RGW audit, outbox loops"] },
    { id: "ceph", title: "Ceph clusters", subtitle: `${activeCeph.length} active · ${profile.ceph_execution_modes.join(" / ") || "mode chưa rõ"}`, status: "core", icon: HardDrive, facts: profile.clusters.length ? profile.clusters.map((cluster) => `${cluster.label}${cluster.default ? " · mặc định" : ""}: ${cluster.active ? "active" : "inactive"}; ${cluster.execution_mode}; MON ${cluster.node_counts.mon ?? 0}, MGR ${cluster.node_counts.mgr ?? 0}, OSD ${cluster.node_counts.osd ?? 0}, RGW ${cluster.node_counts.rgw ?? 0}`) : ["Chưa có Ceph cluster trong database"] },
  ];
  const optional = featureSpecs.map((spec) => {
    const feature = profile.features[spec.key] || { status: "not_configured" as const };
    return { id: spec.key, title: spec.title, subtitle: spec.subtitle, status: feature.status, icon: spec.icon, configHref: spec.configHref, facts: detailsForFeature(spec.key, feature) } satisfies GraphNode;
  });
  const edges: GraphEdge[] = [
    { from: "browser", to: "dashboard", label: "request / response", kind: "core" },
    { from: "dashboard", to: "database", label: "read / write", kind: "core" },
    { from: "watcher", to: "database", label: "state / snapshot", kind: "core" },
    { from: "watcher", to: "rabbit", label: "publish incident", kind: "core" },
    { from: "rabbit", to: "worker", label: "consume task", kind: "core" },
    { from: "worker", to: "database", label: "action / audit", kind: "core" },
    { from: "watcher", to: "ceph", label: "collect metrics", kind: "core" },
    { from: "worker", to: "ceph", label: "approved command", kind: "core" },
  ];
  const configured = (key: string) => profile.features[key]?.status === "configured";
  const add = (key: string, from: string, label = "integration") => edges.push({ from, to: key, label, kind: configured(key) ? "optional" : "unconfigured" });
  add("object_storage", "dashboard", "S3 API / audit"); add("object_storage", "worker", "S3 jobs");
  add("openstack", "dashboard", "Cinder API"); add("openstack", "worker", "volume tasks");
  add("ai_provider", "dashboard", "prompt / response"); add("ai_provider", "worker", "delegated jobs");
  add("backup", "worker", "backup / restore"); add("telegram", "watcher", "alert delivery"); add("telegram", "worker", "approval notice");
  add("log_intelligence", "watcher", "log collection"); add("vitastor", "watcher", "metrics"); add("vitastor", "dashboard", "API");
  add("federated_identity", "dashboard", "identity claims"); add("vault_monitoring", "watcher", "security signals");
  const providerTypes = profile.features.federated_identity?.enabled_provider_types;
  if (configured("federated_identity") && configured("object_storage") && providerTypes && typeof providerTypes === "object" && "oidc" in providerTypes) {
    edges.push({ from: "worker", to: "object_storage", label: "OIDC / S3 identity", kind: "optional" });
  }
  const groups: GraphGroup[] = [
    { id: "control", title: "CONTROL & STATE", nodes: core },
    { id: "processing", title: "COLLECT · QUEUE · PROCESS", nodes: processing },
    { id: "optional", title: "TÙY CHỌN THEO CẤU HÌNH", nodes: optional },
  ];
  groups.forEach((group) => group.nodes.forEach((node) => { node.architecture = profile.architecture?.[node.id]; }));
  return { groups, edges };
}

function formatTimestamp(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Chưa rõ thời điểm";
  return new Intl.DateTimeFormat("vi-VN", {
    day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  }).format(date).replace(",", "");
}

export function InstallationStream({ profile }: { profile: InstallationProfile }) {
  const graph = useMemo(() => makeGraph(profile), [profile]);
  const [refreshing, setRefreshing] = useState(false);
  const nowLabel = formatTimestamp(profile.generated_at);
  const allNodes = useMemo(() => graph.groups.flatMap((group) => group.nodes), [graph]);
  const byId = useMemo(() => new Map(allNodes.map((node) => [node.id, node])), [allNodes]);
  const configuredCount = Object.values(profile.features).filter((feature) => feature.status === "configured").length;
  const missingIntegrations = featureSpecs.filter((spec) => profile.features[spec.key]?.status !== "configured").map((spec) => spec.title);
  const activeComponentCount = allNodes.filter((node) => node.status === "core" || node.status === "configured").length;
  const filters: CanvasFilter[] = [
    { key: "all", label: "Tất cả", node: () => true, edge: () => true },
    { key: "core", label: "Chỉ luồng lõi", node: (node) => node.status === "core", edge: (edge) => edge.kind === "core" },
    { key: "configured", label: "Chỉ tích hợp đã bật", node: (node) => node.status === "core" || node.status === "configured", edge: (edge) => edge.kind !== "unconfigured" },
  ];
  const onRefresh = () => {
    if (refreshing) return;
    setRefreshing(true);
    window.location.reload();
  };
  const renderTraceability = (node: CanvasNode) => {
    const selected = byId.get(node.id);
    if (!selected) return null;
    return (
      <section className="installation-stream__traceability" aria-label="Liên kết kiến trúc và kiểm thử">
        <h3>Ánh xạ kiến trúc</h3>
        {selected.architecture ? <>
          <div className="installation-stream__architecture-ids">{selected.architecture.architecture_ids.map((id) => <code key={id}>{id}</code>)}</div>
          <div className="installation-stream__related-flows"><strong>Luồng nghiệp vụ</strong>{selected.architecture.flows.length ? selected.architecture.flows.map((flow) => <span key={flow.id} title={`Mức độ quan trọng: ${flow.criticality}`}>{flow.id.replace(/^flow\./, "").replace(/_/g, " ")}</span>) : <small>Chưa gắn flow</small>}</div>
          <details className="installation-stream__related-details">
            <summary>Kiểm thử liên quan ({selected.architecture.tests.length})</summary>
            <ul>{selected.architecture.tests.map((test) => <li key={test}><code>{test}</code></li>)}</ul>
          </details>
          <details className="installation-stream__related-details">
            <summary>Liên kết trong manifest ({selected.architecture.relationships.length})</summary>
            <ul>{selected.architecture.relationships.map((edge, index) => <li key={`${edge.from}-${edge.kind}-${edge.to}-${index}`}><code>{edge.from}</code><span>— {edge.kind} →</span><code>{edge.to}</code></li>)}</ul>
          </details>
        </> : <p className="installation-stream__mapping-missing">Thành phần này chưa được ánh xạ trong graph kiến trúc.</p>}
      </section>
    );
  };

  return (
    <div className="installation-stream">
      <header className="installation-stream__header">
        <div>
          <div className="installation-stream__eyebrow">MONITORING &amp; METRICS</div>
          <h1>Luồng cấu hình hệ thống</h1>
          <p>Sơ đồ luồng cài đặt theo cấu hình đã lưu của hệ thống này. Đây là bản đồ tham chiếu, không thực thi hay dò kết nối tới dịch vụ ngoài.</p>
        </div>
        <div className="installation-stream__header-actions">
          <span className="installation-stream__freshness"><Activity size={14} /> Cập nhật {nowLabel}</span>
          <button className="installation-stream__refresh" type="button" onClick={onRefresh} disabled={refreshing}>
            <RefreshCw size={15} className={refreshing ? "is-spinning" : ""} /> {refreshing ? "Đang làm mới…" : "Làm mới"}
          </button>
        </div>
      </header>

      <section className="installation-stream__summary" aria-label="Tổng quan cấu hình">
        <div className="installation-stream__metric"><span>Ceph clusters</span><strong>{profile.clusters.filter((cluster) => cluster.active).length}</strong><small>đang hoạt động</small></div>
        <div className="installation-stream__metric"><span>Chế độ thực thi</span><strong>{profile.ceph_execution_modes.join(" · ") || "Chưa cấu hình"}</strong><small>của các cụm đang hoạt động</small></div>
        <div className="installation-stream__metric installation-stream__integration-metric">
          <div className="installation-stream__metric-line"><span>Tích hợp khả dụng</span><strong>{configuredCount}/{featureSpecs.length}</strong></div>
          <div className="installation-stream__progress" role="progressbar" aria-valuenow={configuredCount} aria-valuemin={0} aria-valuemax={featureSpecs.length}><i style={{ width: `${(configuredCount / featureSpecs.length) * 100}%` }} /></div>
          <small>{missingIntegrations.length ? `Chưa bật: ${missingIntegrations.join(", ")}` : "Tất cả tích hợp đã cấu hình"}</small>
        </div>
        <div className="installation-stream__metric"><span>Thành phần hoạt động</span><strong>{activeComponentCount}<small> / {allNodes.length}</small></strong><small>luồng lõi + tích hợp khả dụng</small></div>
      </section>

      <StreamCanvas
        groups={graph.groups}
        edges={graph.edges}
        defaultPositions={DEFAULT_POSITIONS}
        positionKey={POSITION_KEY}
        statusLabels={{ ...statusLabels, core: "Luồng lõi" }}
        filters={filters}
        legend={[
          { className: "is-core", label: "Luồng dữ liệu chính" },
          { className: "is-configured", label: "Tích hợp tùy chọn" },
          { className: "is-muted", label: "Chưa cấu hình" },
        ]}
        edgeKinds={["core", "optional", "unconfigured"]}
        edgeWidth={(kind) => (kind === "core" ? 2 : kind === "optional" ? 1.5 : 1)}
        kicker="INSTALLATION FLOW"
        title="Luồng runtime & tích hợp"
        ariaLabel="Sơ đồ luồng cài đặt"
        renderDetailExtra={renderTraceability}
      />

      <footer className="installation-stream__note"><Shield size={14} /><span>Profile chỉ phản ánh cấu hình đã lưu; không xác minh kết nối hoặc sức khỏe thực tế.</span><span className="installation-stream__secret-badge"><Check size={13} /> Bí mật đã loại khỏi sơ đồ</span></footer>
    </div>
  );
}
