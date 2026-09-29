import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { PointerEvent, WheelEvent } from "react";
import {
  Activity, Bot, Boxes, Check, Cloud, Database, GitBranch, HardDrive, Magnet,
  Maximize2, RefreshCw, RotateCcw, Server, Shield, Waypoints, Workflow, ZoomIn, ZoomOut,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import "./InstallationStream.css";

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
type GraphNode = {
  id: string;
  title: string;
  subtitle: string;
  status: NodeStatus;
  icon: LucideIcon;
  facts: string[];
  configHref?: string;
  architecture?: ArchitectureLink;
};
type GraphEdge = { from: string; to: string; label: string; kind: "core" | "optional" | "unconfigured" };
type GraphGroup = { id: string; title: string; nodes: GraphNode[] };
type EdgePath = GraphEdge & { d: string; sx: number; sy: number; tx: number; ty: number; mx: number; my: number };
type Point = { x: number; y: number };

const CANVAS = { width: 1120, height: 760 };
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

function roundedOrthogonal(points: number[][], radius: number): string {
  if (points.length < 2) return "";
  let d = `M ${points[0][0]} ${points[0][1]}`;
  for (let index = 1; index < points.length - 1; index += 1) {
    const [px, py] = points[index - 1];
    const [x, y] = points[index];
    const [nx, ny] = points[index + 1];
    const before = Math.min(radius, Math.hypot(x - px, y - py) / 2);
    const after = Math.min(radius, Math.hypot(nx - x, ny - y) / 2);
    const bx = x - Math.sign(x - px) * before;
    const by = y - Math.sign(y - py) * before;
    const ax = x + Math.sign(nx - x) * after;
    const ay = y + Math.sign(ny - y) * after;
    d += ` L ${bx} ${by} Q ${x} ${y} ${ax} ${ay}`;
  }
  const last = points[points.length - 1];
  return `${d} L ${last[0]} ${last[1]}`;
}

type Box = { x: number; y: number; width: number; height: number };
type RoutePoint = [number, number];

function segmentCrossesNode(a: RoutePoint, b: RoutePoint, box: Box): boolean {
  const inset = 5;
  if (Math.abs(a[1] - b[1]) < 0.1) {
    const y = a[1];
    return y > box.y + inset && y < box.y + box.height - inset && Math.max(Math.min(a[0], b[0]), box.x + inset) < Math.min(Math.max(a[0], b[0]), box.x + box.width - inset);
  }
  if (Math.abs(a[0] - b[0]) < 0.1) {
    const x = a[0];
    return x > box.x + inset && x < box.x + box.width - inset && Math.max(Math.min(a[1], b[1]), box.y + inset) < Math.min(Math.max(a[1], b[1]), box.y + box.height - inset);
  }
  return true;
}

function routeAroundNodes(source: Box, target: Box, obstacles: Box[], sourceOffset = 0, targetOffset = 0, canvasWidth = CANVAS.width, canvasHeight = CANVAS.height): RoutePoint[] {
  const sxCenter = source.x + source.width / 2;
  const syCenter = source.y + source.height / 2;
  const txCenter = target.x + target.width / 2;
  const tyCenter = target.y + target.height / 2;
  const candidates: RoutePoint[][] = [];
  if (txCenter >= sxCenter) {
    const sx = source.x + source.width; const tx = target.x;
    const sy = Math.max(source.y + 12, Math.min(source.y + source.height - 12, syCenter + sourceOffset));
    const ty = Math.max(target.y + 12, Math.min(target.y + target.height - 12, tyCenter + targetOffset));
    for (let x = 20; x < canvasWidth; x += 20) candidates.push([[sx, sy], [x, sy], [x, ty], [tx, ty]]);
  } else {
    const sx = source.x; const tx = target.x + target.width;
    const sy = Math.max(source.y + 12, Math.min(source.y + source.height - 12, syCenter + sourceOffset));
    const ty = Math.max(target.y + 12, Math.min(target.y + target.height - 12, tyCenter + targetOffset));
    for (let x = 20; x < canvasWidth; x += 20) candidates.push([[sx, sy], [x, sy], [x, ty], [tx, ty]]);
  }
  const sourceX = Math.max(source.x + 12, Math.min(source.x + source.width - 12, sxCenter + sourceOffset));
  const targetX = Math.max(target.x + 12, Math.min(target.x + target.width - 12, txCenter + targetOffset));
  for (let y = 20; y < canvasHeight; y += 20) {
    candidates.push([[sourceX, source.y + source.height], [sourceX, y], [targetX, y], [targetX, target.y]]);
    candidates.push([[sourceX, source.y], [sourceX, y], [targetX, y], [targetX, target.y + target.height]]);
  }
  const scored = candidates.map((points) => {
    const segments = points.slice(1).map((point, index) => [points[index], point] as const);
    const collisions = segments.reduce((count, [a, b]) => count + obstacles.filter((box) => segmentCrossesNode(a, b, box)).length, 0);
    const length = segments.reduce((sum, [a, b]) => sum + Math.abs(a[0] - b[0]) + Math.abs(a[1] - b[1]), 0);
    return { points, collisions, length };
  });
  scored.sort((a, b) => a.collisions - b.collisions || a.length - b.length);
  return scored[0].points;
}

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
  const diagramRef = useRef<HTMLDivElement>(null);
  const viewportRef = useRef<HTMLDivElement>(null);
  const dragRef = useRef<{ type: "pan" | "node"; id?: string; x: number; y: number; originX: number; originY: number } | null>(null);
  const didDrag = useRef(false);
  const userZoomed = useRef(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [hoveredId, setHoveredId] = useState<string | null>(null);
  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const [positions, setPositions] = useState<Record<string, Point>>(() => {
    try { return { ...DEFAULT_POSITIONS, ...JSON.parse(localStorage.getItem(POSITION_KEY) || "{}") }; }
    catch { return DEFAULT_POSITIONS; }
  });
  const canvasSize = useMemo(() => {
    const placed = Object.values(positions);
    return {
      width: Math.min(4_360, Math.max(CANVAS.width, ...placed.map((point) => point.x + 360))),
      height: Math.min(4_220, Math.max(CANVAS.height, ...placed.map((point) => point.y + 220))),
    };
  }, [positions]);
  const [snapEnabled, setSnapEnabled] = useState(true);
  const [filter, setFilter] = useState<"all" | "core" | "configured">("all");
  const [collapsedGroups, setCollapsedGroups] = useState<Record<string, boolean>>({});
  const [paths, setPaths] = useState<EdgePath[]>([]);
  const [refreshing, setRefreshing] = useState(false);
  const [nowLabel, setNowLabel] = useState(() => formatTimestamp(profile.generated_at));
  const selected = graph.groups.flatMap((group) => group.nodes).find((node) => node.id === selectedId) || null;
  const SelectedIcon = selected?.icon;
  const allNodes = useMemo(() => graph.groups.flatMap((group) => group.nodes), [graph]);
  const configuredCount = Object.values(profile.features).filter((feature) => feature.status === "configured").length;
  const missingIntegrations = featureSpecs.filter((spec) => profile.features[spec.key]?.status !== "configured").map((spec) => spec.title);
  const activeComponentCount = allNodes.filter((node) => node.status === "core" || node.status === "configured").length;
  const focusedId = hoveredId || selectedId;
  const visibleNodeIds = useMemo(() => {
    return new Set(allNodes.filter((node) => {
      const group = graph.groups.find((candidate) => candidate.nodes.some((item) => item.id === node.id));
      if (group && collapsedGroups[group.id]) return false;
      if (filter === "core") return node.status === "core";
      if (filter === "configured") return node.status === "core" || node.status === "configured";
      return true;
    }).map((node) => node.id));
  }, [allNodes, filter, collapsedGroups, graph.groups]);

  useEffect(() => {
    if (selectedId && !visibleNodeIds.has(selectedId)) setSelectedId(null);
  }, [selectedId, visibleNodeIds]);

  const recalculate = useCallback(() => {
    const diagram = diagramRef.current;
    const viewport = viewportRef.current;
    if (!diagram || !viewport) return;
    const diagramRect = diagram.getBoundingClientRect();
    const nodeBoxes = new Map<string, Box>();
    diagram.querySelectorAll<HTMLElement>("[data-node-id]").forEach((element) => {
      const rect = element.getBoundingClientRect();
      nodeBoxes.set(element.dataset.nodeId || "", {
        x: (rect.left - diagramRect.left) / zoom, y: (rect.top - diagramRect.top) / zoom,
        width: rect.width / zoom, height: rect.height / zoom,
      });
    });
    const edgeIndices = new Map<string, number>();
    const targetIndices = new Map<string, number>();
    const visibleEdges = graph.edges.filter((edge) => visibleNodeIds.has(edge.from) && visibleNodeIds.has(edge.to) && (filter !== "core" || edge.kind === "core") && (filter !== "configured" || edge.kind !== "unconfigured"));
    const sourceCounts = new Map<string, number>(); const targetCounts = new Map<string, number>();
    visibleEdges.forEach((edge) => { sourceCounts.set(edge.from, (sourceCounts.get(edge.from) || 0) + 1); targetCounts.set(edge.to, (targetCounts.get(edge.to) || 0) + 1); });
    const nextPaths = visibleEdges.flatMap((edge) => {
      const source = nodeBoxes.get(edge.from);
      const target = nodeBoxes.get(edge.to);
      if (!source || !target) return [];
      const index = edgeIndices.get(edge.from) || 0;
      edgeIndices.set(edge.from, index + 1);
      const targetIndex = targetIndices.get(edge.to) || 0;
      targetIndices.set(edge.to, targetIndex + 1);
      const obstacles = [...nodeBoxes.entries()].filter(([id]) => id !== edge.from && id !== edge.to).map(([, box]) => box);
      const sourceOffset = (index - ((sourceCounts.get(edge.from) || 1) - 1) / 2) * 14;
      const targetOffset = (targetIndex - ((targetCounts.get(edge.to) || 1) - 1) / 2) * 14;
      const route = routeAroundNodes(source, target, obstacles, sourceOffset, targetOffset, canvasSize.width, canvasSize.height);
      const [sx, sy] = route[0];
      const [tx, ty] = route[route.length - 1];
      const midpoint = route[Math.floor(route.length / 2)];
      return [{ ...edge, d: roundedOrthogonal(route, 8), sx, sy, tx, ty, mx: midpoint[0], my: midpoint[1] }];
    });
    setPaths(nextPaths);
  }, [graph.edges, filter, visibleNodeIds, zoom, positions, canvasSize]);

  const fitToScreen = useCallback(() => {
    const viewport = viewportRef.current;
    const diagram = diagramRef.current;
    if (!viewport || !diagram) return;
    const width = Math.max(1, viewport.clientWidth - 24);
    const height = Math.max(1, viewport.clientHeight - 24);
    const contentWidth = canvasSize.width;
    const contentHeight = canvasSize.height;
    userZoomed.current = false;
    setZoom(Math.min(1, width / contentWidth, height / contentHeight));
    setPan({ x: 0, y: 0 });
  }, [canvasSize]);

  useLayoutEffect(() => {
    const frame = window.requestAnimationFrame(() => {
      recalculate();
      if (!userZoomed.current) fitToScreen();
    });
    const observer = new ResizeObserver(() => {
      recalculate();
      if (!userZoomed.current) fitToScreen();
    });
    if (diagramRef.current) observer.observe(diagramRef.current);
    if (viewportRef.current) observer.observe(viewportRef.current);
    return () => { window.cancelAnimationFrame(frame); observer.disconnect(); };
  }, [recalculate, fitToScreen]);

  useLayoutEffect(() => {
    const frame = window.requestAnimationFrame(recalculate);
    return () => window.cancelAnimationFrame(frame);
  }, [recalculate, zoom]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "0" || event.ctrlKey || event.altKey || event.metaKey) return;
      const target = event.target as HTMLElement | null;
      if (target?.closest("input, textarea, select, [contenteditable='true']")) return;
      fitToScreen();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [fitToScreen]);

  const changeZoom = (delta: number) => {
    userZoomed.current = true;
    setZoom((value) => Math.max(0.35, Math.min(1.5, value + delta)));
  };

  const onWheel = (event: WheelEvent<HTMLDivElement>) => {
    if (!event.ctrlKey) return;
    event.preventDefault();
    userZoomed.current = true;
    setZoom((value) => Math.max(0.35, Math.min(1.5, value + (event.deltaY < 0 ? 0.06 : -0.06))));
  };

  const onPointerDown = (event: PointerEvent<HTMLDivElement>) => {
    if (event.button !== 0 || (event.target as HTMLElement).closest("button, a")) return;
    const node = (event.target as HTMLElement).closest<HTMLElement>("[data-node-id]");
    if (node) {
      const id = node.dataset.nodeId || "";
      const point = positions[id] || DEFAULT_POSITIONS[id];
      dragRef.current = { type: "node", id, x: event.clientX, y: event.clientY, originX: point.x, originY: point.y };
      userZoomed.current = true;
      didDrag.current = false;
      node.setPointerCapture(event.pointerId);
    } else {
      setSelectedId(null);
      dragRef.current = { type: "pan", x: event.clientX, y: event.clientY, originX: pan.x, originY: pan.y };
      event.currentTarget.setPointerCapture(event.pointerId);
      event.currentTarget.classList.add("is-panning");
    }
    event.preventDefault();
  };
  const onPointerMove = (event: PointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current;
    if (!drag) return;
    if (drag.type === "pan") setPan({ x: drag.originX + event.clientX - drag.x, y: drag.originY + event.clientY - drag.y });
    else if (drag.id) {
      if (Math.hypot(event.clientX - drag.x, event.clientY - drag.y) > 3) didDrag.current = true;
      const next = {
        x: Math.max(0, Math.min(4_000, drag.originX + (event.clientX - drag.x) / zoom)),
        y: Math.max(0, Math.min(4_000, drag.originY + (event.clientY - drag.y) / zoom)),
      };
      setPositions((value) => ({ ...value, [drag.id!]: next }));
    }
  };
  const onPointerUp = (event: PointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current;
    if (drag?.type === "node" && drag.id) {
      setPositions((value) => {
        const point = value[drag.id!] || DEFAULT_POSITIONS[drag.id!];
        const next = { ...value, [drag.id!]: snapEnabled ? {
          x: Math.max(0, Math.round(point.x / 20) * 20),
          y: Math.max(0, Math.round(point.y / 20) * 20),
        } : point };
        localStorage.setItem(POSITION_KEY, JSON.stringify(next));
        return next;
      });
    }
    dragRef.current = null;
    event.currentTarget.classList.remove("is-panning");
  };
  const onRefresh = () => {
    if (refreshing) return;
    setRefreshing(true);
    window.location.reload();
  };
  const resetLayout = () => { setPositions(DEFAULT_POSITIONS); localStorage.removeItem(POSITION_KEY); setPan({ x: 0, y: 0 }); fitToScreen(); };
  const jumpToNode = (id: string) => { setSelectedId(id); const point = positions[id] || DEFAULT_POSITIONS[id]; setPan({ x: viewportRef.current!.clientWidth / 2 - (point.x + 100) * zoom, y: viewportRef.current!.clientHeight / 2 - (point.y + 40) * zoom }); };

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

      <section className="installation-stream__panel" aria-label="Sơ đồ luồng cài đặt">
        <div className="installation-stream__panel-head">
          <div><span className="installation-stream__section-kicker">INSTALLATION FLOW</span><h2>Luồng runtime &amp; tích hợp</h2></div>
          <div className="installation-stream__zoom" aria-label="Điều khiển sơ đồ" title="Ctrl + cuộn để zoom · 0 để vừa màn hình">
            <button type="button" onClick={fitToScreen} aria-label="Vừa màn hình" title="Vừa màn hình (0)"><Maximize2 size={14} /> <span>Vừa màn hình</span></button>
            <button type="button" onClick={() => changeZoom(-0.1)} aria-label="Thu nhỏ" title="Thu nhỏ"><ZoomOut size={14} /></button>
            <span aria-live="polite">{Math.round(zoom * 100)}%</span>
            <button type="button" onClick={() => changeZoom(0.1)} aria-label="Phóng to" title="Phóng to"><ZoomIn size={14} /></button>
            <button type="button" className={snapEnabled ? "is-active" : ""} onClick={() => setSnapEnabled((value) => !value)} aria-pressed={snapEnabled} title="Bật/tắt bắt lưới 20px"><Magnet size={13} /> Bắt lưới</button>
            <button type="button" onClick={resetLayout} title="Khôi phục bố cục mặc định"><RotateCcw size={13} /> Khôi phục bố cục</button>
          </div>
        </div>
        <div className="installation-stream__legend">
          <div className="installation-stream__filters" role="group" aria-label="Lọc sơ đồ">
            {([["all", "Tất cả"], ["core", "Chỉ luồng lõi"], ["configured", "Chỉ tích hợp đã bật"]] as const).map(([key, label]) => <button type="button" key={key} className={filter === key ? "is-active" : ""} aria-pressed={filter === key} onClick={() => setFilter(key)}>{label}</button>)}
          </div>
          <span className="installation-stream__legend-line"><i className="is-core" /> Luồng dữ liệu chính</span>
          <span className="installation-stream__legend-line"><i className="is-configured" /> Tích hợp tùy chọn</span>
          <span className="installation-stream__legend-line"><i className="is-muted" /> Chưa cấu hình</span>
          <span className="installation-stream__keyboard-hint">Ctrl + cuộn: zoom · Kéo node: sắp xếp · Kéo nền: di chuyển · 0: vừa màn hình</span>
        </div>
        <div className="installation-stream__workspace">
          <div
            className="installation-stream__viewport"
            ref={viewportRef}
            onWheel={onWheel}
            onPointerDown={onPointerDown}
            onPointerMove={onPointerMove}
            onPointerUp={onPointerUp}
            onPointerCancel={onPointerUp}
            aria-label="Sơ đồ luồng; cuộn chuột thường để cuộn trang, Ctrl cộng cuộn để phóng to"
          >
            <div className="installation-stream__stage" style={{ width: `${canvasSize.width * zoom}px`, height: `${canvasSize.height * zoom}px` }}>
              <div className="installation-stream__diagram-transform" style={{ transform: `translate(${pan.x}px, ${pan.y}px) scale(${zoom})` }}>
                <div className="installation-stream__diagram" ref={diagramRef} style={{ width: canvasSize.width, height: canvasSize.height }}>
                  <button type="button" className={`installation-stream__group-label${collapsedGroups.control ? " is-collapsed" : ""}`} style={{ left: 20, top: 20 }} aria-expanded={!collapsedGroups.control} onClick={() => setCollapsedGroups((value) => ({ ...value, control: !value.control }))}>CONTROL &amp; STATE <small>{collapsedGroups.control ? "đã thu gọn" : "3 thành phần"}</small></button>
                  <button type="button" className={`installation-stream__group-label${collapsedGroups.processing ? " is-collapsed" : ""}`} style={{ left: 20, top: 250 }} aria-expanded={!collapsedGroups.processing} onClick={() => setCollapsedGroups((value) => ({ ...value, processing: !value.processing }))}>COLLECT · QUEUE · PROCESS <small>{collapsedGroups.processing ? "đã thu gọn" : "4 thành phần"}</small></button>
                  <button type="button" className={`installation-stream__group-label${collapsedGroups.optional ? " is-collapsed" : ""}`} style={{ left: 20, top: 490 }} aria-expanded={!collapsedGroups.optional} onClick={() => setCollapsedGroups((value) => ({ ...value, optional: !value.optional }))}>TÙY CHỌN THEO CẤU HÌNH <small>{collapsedGroups.optional ? "đã thu gọn" : "9 thành phần"}</small></button>
                  <svg className="installation-stream__edges" aria-label="Các kết nối giữa thành phần" width={canvasSize.width} height={canvasSize.height} viewBox={`0 0 ${canvasSize.width} ${canvasSize.height}`}>
                    <defs>
                      {(["core", "optional", "unconfigured"] as const).map((kind) => <marker key={kind} id={`stream-arrow-${kind}`} viewBox="0 0 10 10" refX="8" refY="5" markerWidth="10" markerHeight="10" orient="auto"><path d="M 0 0 L 10 5 L 0 10 z" className={`arrow-${kind}`} /></marker>)}
                    </defs>
                    {paths.map((edge, index) => {
                      const active = !focusedId || edge.from === focusedId || edge.to === focusedId;
                      const strokeWidth = edge.kind === "core" ? 2 : edge.kind === "optional" ? 1.5 : 1;
                      return <g key={`${edge.from}-${edge.to}-${index}`} className={`installation-stream__edge-group is-${edge.kind}${active ? " is-highlighted" : focusedId ? " is-dimmed" : ""}`} onMouseEnter={() => setHoveredId(edge.from)} onMouseLeave={() => setHoveredId(null)}>
                        <title>{`${allNodes.find((node) => node.id === edge.from)?.title || edge.from} → ${allNodes.find((node) => node.id === edge.to)?.title || edge.to} · ${edge.label}`}</title>
                        <path d={edge.d} strokeWidth={strokeWidth} markerEnd={`url(#stream-arrow-${edge.kind})`} />
                        <circle cx={edge.sx} cy={edge.sy} r="2" />
                        {zoom >= 0.7 && <g className="installation-stream__edge-label"><rect x={edge.mx - Math.max(38, edge.label.length * 3.1)} y={edge.my - 11} width={Math.max(76, edge.label.length * 6.2)} height="18" rx="5" /><text x={edge.mx} y={edge.my + 2}>{edge.label}</text></g>}
                        <path className="installation-stream__edge-hit" d={edge.d} />
                      </g>;
                    })}
                  </svg>
                  {graph.groups.flatMap((group) => group.nodes).filter((node) => visibleNodeIds.has(node.id)).map((node) => {
                            const Icon = node.icon;
                            const selectedNode = selectedId === node.id;
                            const featureStatus = node.status === "core" ? "Luồng lõi" : statusLabels[node.status];
                            const point = positions[node.id] || DEFAULT_POSITIONS[node.id];
                            const connected = focusedId && graph.edges.some((edge) => (edge.from === focusedId || edge.to === focusedId) && (edge.from === node.id || edge.to === node.id));
                            return (
                              <div
                                key={node.id}
                                className={`installation-stream__node is-${node.status}${selectedNode ? " is-selected" : ""}${dragRef.current?.id === node.id ? " is-dragging" : ""}${focusedId && focusedId !== node.id && !connected ? " is-dimmed" : ""}${connected ? " is-connected" : ""}`}
                                data-node-id={node.id}
                                style={{ left: point.x, top: point.y, zIndex: dragRef.current?.id === node.id ? 10 : selectedNode ? 3 : 2 }}
                                role="button"
                                tabIndex={0}
                                title={`${node.title}: ${featureStatus}`}
                                aria-label={`${node.title}: ${featureStatus}`}
                                aria-pressed={selectedNode}
                                onMouseEnter={() => setHoveredId(node.id)}
                                onMouseLeave={() => setHoveredId(null)}
                                onFocus={() => setHoveredId(node.id)}
                                onBlur={() => setHoveredId(null)}
                                onClick={() => { if (!didDrag.current) setSelectedId((value) => value === node.id ? null : node.id); }}
                                onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); setSelectedId(node.id); } }}
                              >
                                <span className="installation-stream__status-dot" aria-hidden="true" />
                                <span className="installation-stream__node-icon"><Icon size={17} /></span>
                                <span className="installation-stream__node-copy"><strong>{node.title}</strong><small>{node.subtitle}</small></span>
                                {node.configHref && <a className="installation-stream__configure" href={node.configHref} onClick={(event) => event.stopPropagation()}>Cấu hình <span aria-hidden="true">→</span></a>}
                              </div>
                            );
                          })}
                </div>
              </div>
            </div>
            <div className="installation-stream__minimap" role="button" tabIndex={0} aria-label="Minimap, nhấn để di chuyển sơ đồ" onPointerDown={(event) => event.stopPropagation()} onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); fitToScreen(); } }} onClick={(event) => { const rect = event.currentTarget.getBoundingClientRect(); const x = ((event.clientX - rect.left) / rect.width) * canvasSize.width; const y = ((event.clientY - rect.top) / rect.height) * canvasSize.height; setPan({ x: viewportRef.current!.clientWidth / 2 - x * zoom, y: viewportRef.current!.clientHeight / 2 - y * zoom }); }}>
              <svg viewBox={`0 0 ${canvasSize.width} ${canvasSize.height}`}><rect width={canvasSize.width} height={canvasSize.height} className="minimap-bg" />
                {paths.map((edge, index) => <path key={index} d={edge.d} className={`minimap-edge is-${edge.kind}`} />)}
                {allNodes.filter((node) => visibleNodeIds.has(node.id)).map((node) => { const p = positions[node.id] || DEFAULT_POSITIONS[node.id]; return <rect key={node.id} x={p.x} y={p.y} width="200" height="82" rx="6" className={`minimap-node is-${node.status}`} />; })}
                <rect className="minimap-view" x={Math.max(0, -pan.x / zoom)} y={Math.max(0, -pan.y / zoom)} width={viewportRef.current ? viewportRef.current.clientWidth / zoom : 0} height={viewportRef.current ? viewportRef.current.clientHeight / zoom : 0} />
              </svg>
            </div>
          </div>

          <aside className="installation-stream__detail" aria-live="polite">
            {selected && SelectedIcon ? <>
              <div className={`installation-stream__detail-icon is-${selected.status}`}><SelectedIcon size={18} /></div>
              <div className="installation-stream__detail-title">{selected.title}<span className={`installation-stream__detail-status is-${selected.status}`}>{statusLabels[selected.status]}</span></div>
              <p className="installation-stream__detail-subtitle">{selected.subtitle}</p>
              <ul>{selected.facts.map((fact) => <li key={fact}>{fact}</li>)}</ul>
              <section className="installation-stream__connections">
                <h3>Nhận dữ liệu từ</h3>
                {graph.edges.filter((edge) => edge.to === selected.id).map((edge) => <button type="button" key={`${edge.from}-${edge.to}-${edge.label}`} onClick={() => jumpToNode(edge.from)}><span>{allNodes.find((node) => node.id === edge.from)?.title || edge.from}</span><small>{edge.label}</small></button>)}
                {!graph.edges.some((edge) => edge.to === selected.id) && <small>Không có luồng vào được khai báo.</small>}
                <h3>Gửi dữ liệu tới</h3>
                {graph.edges.filter((edge) => edge.from === selected.id).map((edge) => <button type="button" key={`${edge.from}-${edge.to}-${edge.label}`} onClick={() => jumpToNode(edge.to)}><span>{allNodes.find((node) => node.id === edge.to)?.title || edge.to}</span><small>{edge.label}</small></button>)}
                {!graph.edges.some((edge) => edge.from === selected.id) && <small>Không có luồng ra được khai báo.</small>}
              </section>
              {selected.configHref && <a className="installation-stream__detail-link" href={selected.configHref}>Mở cấu hình <span aria-hidden="true">→</span></a>}
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
            </> : <div className="installation-stream__empty-detail"><Workflow size={25} /><strong>Chọn một thành phần</strong><span>Thông tin chi tiết sẽ hiển thị tại đây.</span></div>}
          </aside>
        </div>
      </section>

      <footer className="installation-stream__note"><Shield size={14} /><span>Profile chỉ phản ánh cấu hình đã lưu; không xác minh kết nối hoặc sức khỏe thực tế.</span><span className="installation-stream__secret-badge"><Check size={13} /> Bí mật đã loại khỏi sơ đồ</span></footer>
    </div>
  );
}
