import type { LucideIcon } from "lucide-react";
import { Download, Maximize2, Table2 } from "lucide-react";

type PanelHeaderProps = {
  title: string;
  icon: LucideIcon;
  tone?: "default" | "warning" | "success";
  actions?: boolean;
};

export function PanelHeader({ title, icon: Icon, tone = "default", actions = false }: PanelHeaderProps) {
  return (
    <header className={`panel-header panel-header--${tone}`}>
      <div className="panel-title"><Icon size={15} strokeWidth={1.7} aria-hidden="true" /><span>{title}</span></div>
      {actions && <div className="panel-actions" aria-label={`${title} actions`}><Download /><Table2 /><Maximize2 /></div>}
    </header>
  );
}
