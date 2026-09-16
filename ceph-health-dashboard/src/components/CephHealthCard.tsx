import { HeartPulse } from "lucide-react";
import { MetricCard } from "./MetricCard";

export function CephHealthCard({ value }: { value: string }) {
  return <MetricCard title="Ceph Health" icon={HeartPulse} value={value} subtitle="show details" tone="warning" size="lg" />;
}
