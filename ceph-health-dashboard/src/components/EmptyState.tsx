import type { LucideIcon } from "lucide-react";

type EmptyStateProps = {
  message: string;
  hint?: string;
  icon?: LucideIcon;
};

/** "Nothing here" must look different from "still loading" and from "failed";
 *  a bare grey sentence in a table cell read as all three. */
export function EmptyState({ message, hint, icon: Icon }: EmptyStateProps) {
  return (
    <div className="empty-state">
      {Icon && <Icon size={22} strokeWidth={1.5} aria-hidden="true" />}
      <p className="empty-state__message">{message}</p>
      {hint && <p className="empty-state__hint">{hint}</p>}
    </div>
  );
}
