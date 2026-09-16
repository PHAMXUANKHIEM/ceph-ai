import { useEffect, useMemo, useState } from "react";
import { Activity, ChartNoAxesCombined, Gauge, HardDrive, PieChart, RefreshCw, Server, SquareTerminal, Wifi } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { CephHealthCard } from "./CephHealthCard";
import { ErrorState } from "./ErrorState";
import { LoadingState } from "./LoadingState";
import { MetricPanel } from "./MetricPanel";
import { PageHeader } from "./PageHeader";
import { PlacementGroupsCard } from "./PlacementGroupsCard";
import { StatusBadge } from "./StatusBadge";
import { StatusCard } from "./StatusCard";
import { useClusterSnapshotEvents } from "../useClusterSnapshotEvents";

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
  cluster_id?: string;
  generation?: number;
  collected_at?: string | null;
  health_available?: boolean;
  last_error?: string | null;
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

export function CephDashboard() {
  const [health, setHealth] = useState<DashboardHealth>(emptyHealth);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [reloadToken, setReloadToken] = useState(0);
  const [refreshPending, setRefreshPending] = useState(false);
  const [dismissedIssue, setDismissedIssue] = useState<string | null>(null);
  const selectedCluster = new URLSearchParams(window.location.search).get("cluster") || "";
  const clusterName = document.getElementById("ceph-dashboard-root")?.getAttribute("data-cluster-name") || selectedCluster || "Cluster";
  const eventVersion = useClusterSnapshotEvents(selectedCluster);

  const requestRefresh = () => {
    if (refreshPending) return;
    setRefreshPending(true);
    setLoadError(null);
    setDismissedIssue(null);
    const url = selectedCluster
      ? "/api/dashboard/health/refresh?cluster=" + encodeURIComponent(selectedCluster)
      : "/api/dashboard/health/refresh";
    fetch(url, { method: "POST", credentials: "same-origin" })
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
      fetch(url, { credentials: "same-origin", signal: controller.signal })
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

  const issueKey = loadError
    ? `load:${loadError}`
    : health.refreshing ? null
      : health.stale ? `stale:${health.last_error || "snapshot"}`
        : health.last_error ? `sync:${health.last_error}` : null;

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
              tone={loadError ? "critical" : health.stale ? "warning" : "healthy"}
              label={loadError ? "Mất kết nối" : health.stale ? "Dữ liệu cũ" : "Đang kết nối"}
              icon={Wifi}
            />
            <span className="snapshot-time">{formatAge(health.age_seconds)}</span>
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
    </main>
  );
}
