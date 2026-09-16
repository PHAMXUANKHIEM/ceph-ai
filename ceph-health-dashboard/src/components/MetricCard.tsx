import type { LucideIcon } from "lucide-react";
import { PanelHeader } from "./PanelHeader";

type MetricCardProps = {
  title: string;
  icon: LucideIcon;
  value: string;
  subtitle?: string;
  /** Tints both the header and the body — health/placement cards used to
   *  carry their own near-identical rule sets for exactly this. */
  tone?: "default" | "warning" | "success";
  /** Only rendered when a real percentage exists; the roadmap forbids
   *  inventing a bar for a missing value. */
  meter?: number | null;
  size?: "md" | "lg";
  actions?: boolean;
};

export function MetricCard({
  title, icon, value, subtitle, tone = "default", meter, size = "md", actions = false,
}: MetricCardProps) {
  const clamped = meter === undefined || meter === null ? null : Math.max(0, Math.min(100, meter));
  return (
    <article className={`dashboard-card metric-card metric-card--${tone}`}>
      <PanelHeader title={title} icon={icon} tone={tone} actions={actions} />
      <div className={`metric-card__body metric-card__body--${size}`}>
        <strong>{value}</strong>
        {clamped !== null && (
          <span
            className="metric-card__meter"
            role="progressbar"
            aria-label={`${title} utilization`}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={clamped}
          >
            <i style={{ width: `${clamped}%` }} />
          </span>
        )}
        {subtitle && <span className="metric-card__subtitle">{subtitle}</span>}
      </div>
    </article>
  );
}
