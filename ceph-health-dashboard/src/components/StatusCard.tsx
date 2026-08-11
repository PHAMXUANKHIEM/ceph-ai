import type { LucideIcon } from "lucide-react";
import { PanelHeader } from "./PanelHeader";

type StatusCardProps = {
  title: string;
  value: string;
  subtitle: string;
  icon: LucideIcon;
};

export function StatusCard({ title, value, subtitle, icon }: StatusCardProps) {
  return (
    <article className="dashboard-card status-card">
      <PanelHeader title={title} icon={icon} />
      <div className="status-card__body"><strong>{value}</strong><span>{subtitle}</span></div>
    </article>
  );
}
