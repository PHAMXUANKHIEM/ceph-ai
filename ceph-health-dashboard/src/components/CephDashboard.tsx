import { useEffect, useMemo, useState } from "react";
import { ChartNoAxesCombined, Gauge, HardDrive, PieChart, Server, SquareTerminal } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { CephHealthCard } from "./CephHealthCard";
import { MetricPanel } from "./MetricPanel";
import { PlacementGroupsCard } from "./PlacementGroupsCard";
import { StatusCard } from "./StatusCard";

type StatusDatum = { title: string; value: string; subtitle: string; icon: LucideIcon };
type DashboardHealth = {
  health: string;
  osds: { up: number | null; total: number | null };
  mons: { up: number | null; total: number | null };
  servers: { online: number | null; total: number | null };
  utilization: { percent: number | null; bytes_used: number | null; pools: number | null };
  metrics: { latency_ms: number | null; bandwidth_bps: number | null; iops: number | null };
  placement_groups: string;
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

export function CephDashboard() {
  const [health, setHealth] = useState<DashboardHealth>(emptyHealth);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [reloadToken, setReloadToken] = useState(0);

  useEffect(() => {
    let controller: AbortController | null = null;
    const load = () => {
      controller?.abort();
      controller = new AbortController();
      const cluster = new URLSearchParams(window.location.search).get("cluster");
      const url = cluster ? "/api/dashboard/health?cluster=" + encodeURIComponent(cluster) : "/api/dashboard/health";
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
    // Metrics remain live without constantly repainting the dashboard. A
    // WebSocket incident event still requests an immediate refresh. Polling
    // pauses entirely while the tab is hidden — the /api/dashboard/health
    // endpoint is SSH-backed, so a backgrounded tab shouldn't keep paying for
    // it — and refreshes once the moment the operator returns to the tab.
    const tick = () => { if (!document.hidden) load(); };
    const onVisible = () => { if (!document.hidden) load(); };
    const timer = window.setInterval(tick, 30_000);
    window.addEventListener("ceph-dashboard-refresh", load);
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener("ceph-dashboard-refresh", load);
      document.removeEventListener("visibilitychange", onVisible);
      controller?.abort();
    };
  }, [reloadToken]);

  const statusCards = useMemo<StatusDatum[]>(() => [
    { title: "OSDs", value: ratio(health.osds.up, health.osds.total), subtitle: "Up", icon: HardDrive },
    { title: "MONs", value: ratio(health.mons.up, health.mons.total), subtitle: "Quorum", icon: SquareTerminal },
    { title: "Servers", value: ratio(health.servers.online, health.servers.total), subtitle: "Online", icon: Server },
    {
      title: "Utilization",
      value: health.utilization.percent === null ? "—" : String(health.utilization.percent) + "%",
      subtitle: formatUsed(health.utilization.bytes_used) + " in " + String(health.utilization.pools ?? "—") + " pools",
      icon: PieChart
    }
  ], [health]);

  return (
    <main className="ceph-dashboard">
      {loadError && (
        <div className="dashboard-live-error" role="alert">
          <span><strong>Không tải được dữ liệu cụm đã chọn.</strong> {loadError}</span>
          <button type="button" onClick={() => setReloadToken((value) => value + 1)}>Thử lại</button>
        </div>
      )}
      <section className="status-grid" aria-label="Ceph status overview">
        <CephHealthCard value={health.health} />
        {statusCards.map((card) => <StatusCard key={card.title} {...card} />)}
      </section>
      <section className="metrics-grid" aria-label="Ceph performance metrics">
        <MetricPanel title="Latency" icon={Gauge} value={health.metrics.latency_ms === null ? "—" : health.metrics.latency_ms.toFixed(2) + " ms"} subtitle="OSD average" />
        <MetricPanel title="Bandwidth" icon={ChartNoAxesCombined} value={formatRate(health.metrics.bandwidth_bps)} subtitle="Read + write" />
        <MetricPanel title="IOPS" icon={ChartNoAxesCombined} value={formatIops(health.metrics.iops)} subtitle="Read + write ops/s" />
        <PlacementGroupsCard value={health.placement_groups} />
      </section>
    </main>
  );
}
