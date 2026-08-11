import type { LucideIcon } from "lucide-react";
import { PanelHeader } from "./PanelHeader";

type MetricPanelProps = { title: string; icon: LucideIcon; value: string; subtitle?: string };

export function MetricPanel({ title, icon, value, subtitle }: MetricPanelProps) {
  return (
    <article className="dashboard-card metric-panel">
      <PanelHeader title={title} icon={icon} actions />
      <div className="metric-panel__body">
        <strong>{value}</strong>
        {subtitle && <span>{subtitle}</span>}
      </div>
    </article>
  );
}
