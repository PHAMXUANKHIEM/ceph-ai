import type { LucideIcon } from "lucide-react";

export type StatusTone = "healthy" | "warning" | "critical" | "neutral";

type StatusBadgeProps = {
  tone: StatusTone;
  label: string;
  icon?: LucideIcon;
};

/** The single place a state maps to a colour, so the same state never reads
 *  green on one page and grey on another. */
export function StatusBadge({ tone, label, icon: Icon }: StatusBadgeProps) {
  return (
    <span className={`status-badge status-badge--${tone}`}>
      {Icon && <Icon size={14} strokeWidth={1.8} aria-hidden="true" />}
      {label}
    </span>
  );
}
