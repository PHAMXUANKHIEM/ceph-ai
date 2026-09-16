import { Workflow } from "lucide-react";
import { MetricCard } from "./MetricCard";

export function PlacementGroupsCard({ value }: { value: string }) {
  return <MetricCard title="Placement Groups" icon={Workflow} value={value} tone="success" size="lg" />;
}
