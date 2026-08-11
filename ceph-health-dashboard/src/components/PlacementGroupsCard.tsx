import { Workflow } from "lucide-react";
import { PanelHeader } from "./PanelHeader";

export function PlacementGroupsCard({ value }: { value: string }) {
  return (
    <article className="dashboard-card placement-card">
      <PanelHeader title="Placement Groups" icon={Workflow} tone="success" />
      <div className="placement-card__body">{value}</div>
    </article>
  );
}
