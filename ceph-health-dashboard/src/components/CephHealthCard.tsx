import { HeartPulse } from "lucide-react";
import { PanelHeader } from "./PanelHeader";

export function CephHealthCard({ value }: { value: string }) {
  return (
    <article className="dashboard-card health-card">
      <PanelHeader title="Ceph Health" icon={HeartPulse} tone="warning" />
      <div className="status-card__body"><strong>{value}</strong><span>show details</span></div>
    </article>
  );
}
