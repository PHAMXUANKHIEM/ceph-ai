import { useCallback, useEffect, useMemo, useState } from "react";
import { Activity, AlertTriangle, ChartNoAxesCombined, Clock3, Database, Gauge, HardDrive, PieChart, RefreshCw, Server, ShieldCheck, SquareTerminal, Wifi } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { CephHealthCard } from "./CephHealthCard";
import { ErrorState } from "./ErrorState";
import { LoadingState } from "./LoadingState";
import { MetricPanel } from "./MetricPanel";
import { PageHeader } from "./PageHeader";
import { PlacementGroupsCard } from "./PlacementGroupsCard";
import { StatusBadge } from "./StatusBadge";
import { StatusCard } from "./StatusCard";
import { useClusterSnapshotEvents, type SnapshotEvent } from "../useClusterSnapshotEvents";
import { getSnapshotState, SNAPSHOT_STATE_LABEL } from "../snapshotState";

type StatusDatum = { title: string; value: string; subtitle: string; icon: LucideIcon; meter?: number | null };
type DashboardHealth = {
  health: string;
  osds: { up: number | null; total: number | null };
  mons: { up: number | null; total: number | null };
  servers: { online: number | null; total: number | null };
  utilization: { percent: number | null; bytes_used: number | null; pools: number | null };
  metrics: { latency_ms: number | null; bandwidth_bps: number | null; iops: number | null };
  placement_groups: string;
  cached?: boolean;
  stale?: boolean;
  refreshing?: boolean;
  age_seconds?: number | null;
  cache_age_seconds?: number;
  collector_lag_seconds?: number | null;
  cluster_id?: string;
  generation?: number;
  collected_at?: string | null;
  health_available?: boolean;
  last_error?: string | null;
  partial_errors?: Record<string, unknown>;
};

const emptyHealth: DashboardHealth = {
  health: "UNKNOWN",
  osds: { up: null, total: null },
  mons: { up: null, total: null },
  servers: { online: null, total: null },
  utilization: { percent: null, bytes_used: null, pools: null },
  metrics: { latency_ms: null, bandwidth_bps: null, iops: null },
  placement_groups: "UNKNOWN"
};

const ratio = (up: number | null, total: number | null) => String(up ?? "—") + "/" + String(total ?? "—");
const formatUsed = (bytes: number | null) => bytes === null ? "—" : (bytes / 1_000_000_000).toFixed(2) + " GB";
const formatRate = (bytes: number | null) => {
  if (bytes === null) return "—";
  if (bytes >= 1_000_000_000) return (bytes / 1_000_000_000).toFixed(2) + " GB/s";
  if (bytes >= 1_000_000) return (bytes / 1_000_000).toFixed(2) + " MB/s";
  if (bytes >= 1_000) return (bytes / 1_000).toFixed(2) + " KB/s";
  return Math.round(bytes) + " B/s";
};
const formatIops = (iops: number | null) => iops === null ? "—" : Math.round(iops).toLocaleString();
const formatAge = (age: number | null | undefined) => {
  if (age === null || age === undefined) return "chưa có snapshot";
  if (age < 60) return `${Math.max(0, Math.round(age))} giây trước`;
  const minutes = Math.floor(age / 60);
  if (minutes < 60) return `${minutes} phút trước`;
  return `${Math.floor(minutes / 60)} giờ trước`;
};

const formatSnapshotState = (health: DashboardHealth) => {
  return SNAPSHOT_STATE_LABEL[getSnapshotState(health, { hasData: Boolean(health.collected_at) })];
};

const formatErrorValue = (value: unknown) => {
  if (typeof value === "string") return value;
  if (value && typeof value === "object" && "message" in value) return String((value as { message?: unknown }).message || "Lỗi thu thập");
  return "Lỗi thu thập dữ liệu";
};

export function CephDashboard() {
  const [health, setHealth] = useState<DashboardHealth>(emptyHealth);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [reloadToken, setReloadToken] = useState(0);
  const [refreshPending, setRefreshPending] = useState(false);
  const [dismissedIssue, setDismissedIssue] = useState<string | null>(null);
  const [realtimeError, setRealtimeError] = useState<string | null>(null);
  const [actionState, setActionState] = useState<string | null>(null);
  const selectedCluster = new URLSearchParams(window.location.search).get("cluster") || "";
  const clusterName = document.getElementById("ceph-dashboard-root")?.getAttribute("data-cluster-name") || selectedCluster || "Cluster";
  const handleRealtimeEvent = useCallback((event: SnapshotEvent) => {
    if (event.event === "snapshot_refresh_failed") {
      const action = event.action_id ? ` (action ${event.action_id})` : "";
      setRealtimeError(`Post-check mutation thất bại${action}; đang giữ snapshot tốt gần nhất.`);
    } else if (event.event === "action_state_changed") {
      setActionState(event.action_state || event.action_status || "updated");
    } else if (event.event === "snapshot_changed") {
      setRealtimeError(null);
      setActionState(null);
    }
  }, []);
  const eventVersion = useClusterSnapshotEvents(selectedCluster, handleRealtimeEvent);

  const requestRefresh = () => {
    if (refreshPending) return;
    setRefreshPending(true);
    setLoadError(null);
    setDismissedIssue(null);
    const url = selectedCluster
      ? "/api/dashboard/health/refresh?cluster=" + encodeURIComponent(selectedCluster)
      : "/api/dashboard/health/refresh";
    fetch(url, { method: "POST", credentials: "same-origin", headers: { "X-Request-ID": `browser-${Date.now()}-${Math.random().toString(36).slice(2, 10)}` } })
      .then(async (response) => {
        if (!response.ok) throw new Error("HTTP " + response.status);
      })
      .then(() => setReloadToken((value) => value + 1))
      .catch((error: unknown) => {
        setLoadError(error instanceof Error ? error.message : "Không thể yêu cầu đồng bộ");
      })
      .finally(() => setRefreshPending(false));
  };

  useEffect(() => {
    let controller: AbortController | null = null;
    const load = () => {
      if (document.hidden) return;
      controller?.abort();
      controller = new AbortController();
      const url = selectedCluster ? "/api/dashboard/health?cluster=" + encodeURIComponent(selectedCluster) : "/api/dashboard/health";
      fetch(url, { credentials: "same-origin", signal: controller.signal, headers: { "X-Request-ID": `browser-${Date.now()}-${Math.random().toString(36).slice(2, 10)}` } })
      .then(async (response) => {
        if (!response.ok) {
          let detail = "HTTP " + response.status;
          try {
            const body = await response.json() as { detail?: string };
            if (body.detail) detail = body.detail;
          } catch {
            // A proxy may return HTML/plain text. The status still gives the
            // operator a useful, visible failure instead of an empty board.
          }
          throw new Error(detail);
        }
        return response.json() as Promise<DashboardHealth>;
      })
        .then((next) => {
          setLoadError(null);
          setHealth((current) => JSON.stringify(current) === JSON.stringify(next) ? current : next);
        })
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === "AbortError") return;
        console.error(error);
        setLoadError(error instanceof Error ? error.message : "Không xác định được lỗi kết nối");
      });
    };
    load();
    const onVisibilityChange = () => { if (!document.hidden) load(); };
    // The API returns the persisted snapshot immediately. The Watcher owns
    // normal collection; this short read-only poll only picks up new
    // generations and freshness metadata.
    const timer = window.setInterval(load, 5_000);
    window.addEventListener("ceph-dashboard-refresh", load);
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener("ceph-dashboard-refresh", load);
      document.removeEventListener("visibilitychange", onVisibilityChange);
      controller?.abort();
    };
  }, [eventVersion, reloadToken, selectedCluster]);

  const statusCards = useMemo<StatusDatum[]>(() => [
    { title: "OSDs", value: ratio(health.osds.up, health.osds.total), subtitle: "Up", icon: HardDrive },
    { title: "MONs", value: ratio(health.mons.up, health.mons.total), subtitle: "Quorum", icon: SquareTerminal },
    { title: "Servers", value: ratio(health.servers.online, health.servers.total), subtitle: "Online", icon: Server },
    {
      title: "Utilization",
      value: health.utilization.percent === null ? "—" : String(health.utilization.percent) + "%",
      subtitle: formatUsed(health.utilization.bytes_used) + " in " + String(health.utilization.pools ?? "—") + " pools",
      icon: PieChart, meter: health.utilization.percent
    }
  ], [health]);

  const issueKey = realtimeError
    ? `realtime:${realtimeError}`
    : loadError
    ? `load:${loadError}`
    : health.refreshing ? null
      : health.stale ? `stale:${health.last_error || "snapshot"}`
        : health.last_error ? `sync:${health.last_error}` : null;
  const collectionErrors = useMemo(() => {
    const entries = Object.entries(health.partial_errors || {});
    if (health.last_error && !entries.some(([key]) => key === "snapshot")) entries.unshift(["snapshot", health.last_error]);
    return entries.slice(0, 4);
  }, [health.last_error, health.partial_errors]);
  const snapshotState = getSnapshotState(health, {
    error: loadError,
    hasData: Boolean(health.collected_at),
  });

  return (
    <main className="ceph-dashboard">
      <PageHeader
        eyebrow="CEPH OPERATIONS CONSOLE"
        title="Cluster overview"
        breadcrumb={<span className="page-header__breadcrumb">› {clusterName}</span>}
        subtitle="Health, capacity and performance at a glance"
        actions={
          <>
            <StatusBadge
              tone={snapshotState === "error" ? "critical" : snapshotState === "stale" || snapshotState === "refreshing" ? "warning" : snapshotState === "fresh" ? "healthy" : "neutral"}
              label={SNAPSHOT_STATE_LABEL[snapshotState]}
              icon={Wifi}
            />
            <span className="snapshot-time">{formatAge(health.age_seconds)}{health.collector_lag_seconds != null ? ` · collector lag ${health.collector_lag_seconds.toFixed(2)}s` : ""}</span>
            <button className="dashboard-refresh" type="button" onClick={requestRefresh} disabled={refreshPending} aria-label="Làm mới dữ liệu">
              <RefreshCw size={15} className={refreshPending ? "dashboard-spin" : ""} aria-hidden="true" /> Làm mới
            </button>
          </>
        }
      />
      {loadError && dismissedIssue !== issueKey && (
        <ErrorState
          message={<><strong>Không tải được dữ liệu cụm đã chọn.</strong> {loadError}</>}
          onRetry={() => setReloadToken((value) => value + 1)}
          onDismiss={() => setDismissedIssue(issueKey)}
        />
      )}
      {realtimeError && dismissedIssue !== issueKey && (
        <ErrorState
          tone="warning"
          message={<><strong>Thay đổi cụm chưa được xác nhận.</strong> {realtimeError}</>}
          onRetry={() => setReloadToken((value) => value + 1)}
          retryLabel="Đọc lại snapshot"
          onDismiss={() => setDismissedIssue(issueKey)}
        />
      )}
      {actionState && <div className="mb-3 rounded-md border border-sky-700/50 bg-sky-950/30 px-3 py-2 text-sm text-sky-200" role="status" aria-live="polite">Cập nhật action: <strong>{actionState}</strong>. Dữ liệu sẽ được đọc lại từ snapshot sau khi collector xác nhận.</div>}
      {health.refreshing && <LoadingState message="Đang đồng bộ dữ liệu cụm…" />}
      {!health.refreshing && health.stale && dismissedIssue !== issueKey && (
        <ErrorState
          tone="warning"
          message={<>Dữ liệu đang cũ — {formatAge(health.age_seconds)}{health.last_error && <><br />Lỗi đồng bộ gần nhất: {health.last_error}</>}</>}
          onRetry={requestRefresh}
          retryLabel="Đồng bộ lại"
          retryDisabled={refreshPending}
          onDismiss={() => setDismissedIssue(issueKey)}
        />
      )}
      {!health.refreshing && !health.stale && health.last_error && dismissedIssue !== issueKey && (
        <ErrorState
          message={<>Lần đồng bộ gần nhất thất bại: {health.last_error}</>}
          onRetry={requestRefresh}
          retryLabel="Đồng bộ lại"
          retryDisabled={refreshPending}
          onDismiss={() => setDismissedIssue(issueKey)}
        />
      )}
      <section className="status-grid" aria-label="Ceph status overview">
        <CephHealthCard value={health.health} />
        {statusCards.map((card) => <StatusCard key={card.title} {...card} />)}
      </section>
      <div className="dashboard-section-label"><Activity size={15} aria-hidden="true" /> Performance snapshot</div>
      <section className="metrics-grid" aria-label="Ceph performance metrics">
        <MetricPanel title="Latency" icon={Gauge} value={health.metrics.latency_ms === null ? "N/A" : health.metrics.latency_ms.toFixed(2) + " ms"} subtitle="OSD average" />
        <MetricPanel title="Bandwidth" icon={ChartNoAxesCombined} value={formatRate(health.metrics.bandwidth_bps)} subtitle="Read + write" />
        <MetricPanel title="IOPS" icon={ChartNoAxesCombined} value={formatIops(health.metrics.iops)} subtitle="Read + write ops/s" />
        <PlacementGroupsCard value={health.placement_groups} />
      </section>
      <section className="dashboard-operations" aria-label="Tín hiệu vận hành">
        <article className="dashboard-operations-panel">
          <header className="dashboard-operations-header">
            <div>
              <span className="dashboard-operations-eyebrow">OPERATIONAL SNAPSHOT</span>
              <h2>Tín hiệu vận hành</h2>
            </div>
            <span className="dashboard-operations-note">Chỉ dùng dữ liệu snapshot hiện có</span>
          </header>
          <div className="dashboard-signal-grid">
            <div className="dashboard-signal-card">
              <Clock3 size={16} aria-hidden="true" />
              <span>Snapshot</span>
              <strong>{formatAge(health.age_seconds)}</strong>
              <small>{health.generation ? `Generation #${health.generation}` : "Chưa có generation"}</small>
            </div>
            <div className="dashboard-signal-card">
              <Database size={16} aria-hidden="true" />
              <span>Placement Groups</span>
              <strong>{health.placement_groups || "—"}</strong>
              <small>{health.utilization.pools ?? "—"} pools đang được theo dõi</small>
            </div>
            <div className="dashboard-signal-card">
              <ShieldCheck size={16} aria-hidden="true" />
              <span>Dung lượng đã dùng</span>
              <strong>{formatUsed(health.utilization.bytes_used)}</strong>
              <small>{health.utilization.percent === null ? "Chưa có tỷ lệ sử dụng" : `${health.utilization.percent}% capacity`}</small>
            </div>
            <div className="dashboard-signal-card">
              <Activity size={16} aria-hidden="true" />
              <span>Thu thập dữ liệu</span>
              <strong>{formatSnapshotState(health)}</strong>
              <small>{health.health_available === false ? "Health snapshot chưa sẵn sàng" : "Watcher cung cấp snapshot gần nhất"}</small>
            </div>
          </div>
          <div className="dashboard-history-note" role="note">
            <Activity size={15} aria-hidden="true" />
            <span>CPU/RAM và Disk trend sẽ xuất hiện khi backend cung cấp dữ liệu chuỗi thời gian. Hiện tại không dựng biểu đồ từ snapshot đơn.</span>
          </div>
        </article>
        <article className="dashboard-operations-panel dashboard-alerts-panel">
          <header className="dashboard-operations-header">
            <div>
              <span className="dashboard-operations-eyebrow">COLLECTION EVENTS</span>
              <h2>Sự cố thu thập gần đây</h2>
            </div>
            <AlertTriangle size={17} aria-hidden="true" />
          </header>
          {collectionErrors.length > 0 ? (
            <ul className="dashboard-alert-list">
              {collectionErrors.map(([source, error]) => (
                <li key={source}>
                  <AlertTriangle size={15} aria-hidden="true" />
                  <span><strong>{source}</strong><small>{formatErrorValue(error)}</small></span>
                </li>
              ))}
            </ul>
          ) : (
            <div className="dashboard-alerts-empty">
              <ShieldCheck size={22} aria-hidden="true" />
              <strong>Chưa ghi nhận lỗi thu thập</strong>
              <span>Dashboard đang dùng snapshot gần nhất của Watcher.</span>
            </div>
          )}
        </article>
      </section>
    </main>
  );
}
