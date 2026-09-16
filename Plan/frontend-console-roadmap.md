# Ceph Operations Console Frontend Roadmap

Target: `/root/ceph-ai`

## Working rules

- Preserve Jinja/JavaScript, React 18, TypeScript, Tailwind, Vite and lucide-react.
- Preserve API contracts, authentication, RBAC, cluster scope, polling/events, AI chat and preview/approval/audit flows.
- Change React source first, then build into `dashboard/static/ceph-health/` with Node 20.
- Do not invent data, charts, backend fields or mutation behavior.
- Complete and verify one item before starting the next.
- Do not push, merge or deploy.

## Ordered work items

### 1. Design tokens and shared shell

- Consolidate the dark operations-console tokens in the shared CSS layer.
- Convert the shared navigation into a real responsive sidebar with grouped sections.
- Add a 60px topbar with breadcrumb, cluster context, freshness state, AI entry point and account actions where the existing templates support them.
- Preserve every existing route, permission condition, product boundary and cluster query parameter.
- Add keyboard focus, reduced-motion support and accessible collapse/drawer behavior.

Done when: all existing navigation links remain reachable, desktop sidebar works at 1440/1920px, collapsed/tablet mode works at 768/1024px, mobile navigation works at 390px, and navigation/template tests pass.

### 2. Dashboard information hierarchy

- Rework the React dashboard into a compact page header, health/status row, KPI row, performance row and operations area.
- Keep snapshot polling and event invalidation unchanged.
- Put stale, refreshing and connection errors in normal document flow.
- Show missing values as `—` and render utilization with a progress bar only when a real percentage exists.
- Do not create trend charts without time-series data.

Done when: health, OSD, MON, server, utilization, latency, bandwidth, IOPS and PG data remain accurate and the dashboard has no fixed-height empty panels.

### 3. Shared UI primitives

- Add reusable React components for PageHeader, MetricCard, StatusBadge, LoadingState, ErrorState and EmptyState.
- Reuse the existing panel and icon conventions in Dashboard and Pools.
- Keep Jinja pages on shared CSS classes where React is not involved.

Done when: Dashboard and Pools share the same spacing, typography, status semantics and focus states without duplicate component styling.

### 4. Tables and administration pages

- Standardize headers, toolbars, table density, numeric alignment, truncation, horizontal scrolling and pagination.
- Apply the system to Nodes, Pools, PGs, Volumes, Block Storage, Object Storage, Backups and admin pages.
- Preserve all existing confirmation, preview, RBAC and audit behavior.

Done when: each page uses only controls backed by its current API and no mutation flow changes.

### 5. AI Assistant panel

- Inspect `chat_widget.js` and existing chat templates before styling.
- Create a right-side responsive panel with readable Markdown, tables, code blocks and copy actions.
- Preserve sessions, history, chat/task modes, stop behavior, connection states and approval gates.
- Use a full-screen mobile presentation with focus return on close.

Done when: chat, task delegation, stop, reconnect and approval interactions pass existing tests and keyboard checks.

### 6. Log Intelligence presentation

- Make OK/PARTIAL/FAILED collection outcomes prominent.
- Preserve source, time window, failed nodes and completeness metadata.
- Improve long-log viewing with scoped scrolling and monospace formatting.
- Keep unverified AI conclusions visually distinct from evidence.

Done when: no collection failure or partial result is hidden and no unsupported filter/drill-down is added.

### 7. Accessibility and responsive verification

- Verify 1920, 1440, 1024, 768 and 390px layouts.
- Verify keyboard navigation, focus-visible, labels, dialogs, drawers, focus return and reduced motion.
- Fix page-level horizontal overflow while retaining table/code scrolling inside their containers.

Done when: screenshots or browser evidence cover Dashboard, Pools, Object Storage, Log Intelligence and AI panel at desktop and mobile widths.

### 8. Regression and handoff

- Run TypeScript/Vite build with Node 20, JavaScript syntax checks and relevant Python tests.
- Verify cluster switching, reload, table filtering, chat and approval presentation without executing administrative commands against a real Ceph cluster.
- Record changed files, checks, known limitations and visual evidence.

Done when: the working tree is reviewable, tests are recorded and no push/merge/deploy has occurred.

## Progress

- [x] 1. Design tokens and shared shell
- [x] 2. Dashboard information hierarchy
- [x] 3. Shared UI primitives
- [ ] 4. Tables and administration pages
- [ ] 5. AI Assistant panel
- [ ] 6. Log Intelligence presentation
- [ ] 7. Accessibility and responsive verification
- [ ] 8. Regression and handoff
