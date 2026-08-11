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

  useEffect(() => {
    let controller: AbortController | null = null;
    const load = () => {
      controller?.abort();
      controller = new AbortController();
      const cluster = new URLSearchParams(window.location.search).get("cluster");
      const url = cluster ? "/api/dashboard/health?cluster=" + encodeURIComponent(cluster) : "/api/dashboard/health";
      fetch(url, { credentials: "same-origin", signal: controller.signal })
      .then(async (response) => {
        if (!response.ok) throw new Error("Dashboard health API returned " + response.status);
        return response.json() as Promise<DashboardHealth>;
      })
      .then(setHealth)
      .catch((error: unknown) => {
        if (!(error instanceof DOMException && error.name === "AbortError")) console.error(error);
      });
    };
    load();
    const timer = window.setInterval(load, 10_000);
    window.addEventListener("ceph-dashboard-refresh", load);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener("ceph-dashboard-refresh", load);
      controller?.abort();
    };
  }, []);

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
