import type { LucideIcon } from "lucide-react";
import { MetricCard } from "./MetricCard";

type MetricPanelProps = { title: string; icon: LucideIcon; value: string; subtitle?: string };

export function MetricPanel({ title, icon, value, subtitle }: MetricPanelProps) {
  return <MetricCard title={title} icon={icon} value={value} subtitle={subtitle} actions />;
}
