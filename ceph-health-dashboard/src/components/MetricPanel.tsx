import type { LucideIcon } from "lucide-react";
import { PanelHeader } from "./PanelHeader";

type MetricPanelProps = { title: string; icon: LucideIcon };

export function MetricPanel({ title, icon }: MetricPanelProps) {
  return (
    <article className="dashboard-card metric-panel">
      <PanelHeader title={title} icon={icon} actions />
      <div className="metric-panel__body">NO DATA</div>
    </article>
  );
}
