import type { LucideIcon } from "lucide-react";
import { MetricCard } from "./MetricCard";

type StatusCardProps = {
  title: string;
  value: string;
  subtitle: string;
  icon: LucideIcon;
  meter?: number | null;
};

/** Kept as the dashboard's status-row name; the geometry now lives in
 *  MetricCard so status/metric/health/placement cards cannot drift apart. */
export function StatusCard({ title, value, subtitle, icon, meter }: StatusCardProps) {
  return <MetricCard title={title} icon={icon} value={value} subtitle={subtitle} meter={meter} size="lg" />;
}
