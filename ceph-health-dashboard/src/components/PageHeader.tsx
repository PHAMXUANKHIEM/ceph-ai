import type { ReactNode } from "react";

type PageHeaderProps = {
  title: string;
  eyebrow?: string;
  subtitle?: string;
  breadcrumb?: ReactNode;
  actions?: ReactNode;
};

/**
 * One page title block for every React page.
 *
 * Dashboard and Pools each had their own: Dashboard used `.dashboard-hero`
 * while Pools used raw Tailwind slate classes that `styles.css` then had to
 * override back to the dark palette. Same element, two spellings, two places
 * to fix whenever the palette moves.
 */
export function PageHeader({ title, eyebrow, subtitle, breadcrumb, actions }: PageHeaderProps) {
  return (
    <header className="page-header">
      <div className="page-header__text">
        {eyebrow && <p className="page-header__eyebrow">{eyebrow}</p>}
        <div className="page-header__headline">
          <h1>{title}</h1>
          {breadcrumb}
        </div>
        {subtitle && <p className="page-header__subtitle">{subtitle}</p>}
      </div>
      {actions && <div className="page-header__actions">{actions}</div>}
    </header>
  );
}
