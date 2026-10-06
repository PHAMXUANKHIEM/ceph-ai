import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { PointerEvent, ReactNode, WheelEvent } from "react";
import { Magnet, Maximize2, RotateCcw, Workflow, ZoomIn, ZoomOut } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import "./InstallationStream.css";

// Shared drawing surface of the Stream page: node layout with drag and
// snap, obstacle-avoiding edge routing, zoom/pan, minimap and the detail
// panel. Each stream (Ceph AI system, Ceph cluster) supplies its graph,
// default layout, filters and legend.

export type CanvasNode = {
  id: string;
  title: string;
  subtitle: string;
  status: string;
  icon: LucideIcon;
  facts: string[];
  configHref?: string;
};
export type CanvasEdge = { from: string; to: string; label: string; kind: string };
export type CanvasGroup = { id: string; title: string; nodes: CanvasNode[] };
export type CanvasFilter = {
  key: string;
  label: string;
  node: (node: CanvasNode) => boolean;
  edge: (edge: CanvasEdge) => boolean;
};
export type Point = { x: number; y: number };
type EdgePath = CanvasEdge & { d: string; sx: number; sy: number; tx: number; ty: number; mx: number; my: number };

export const CANVAS = { width: 1120, height: 760 };

function roundedOrthogonal(points: number[][], radius: number): string {
  if (points.length < 2) return "";
  let d = `M ${points[0][0]} ${points[0][1]}`;
  for (let index = 1; index < points.length - 1; index += 1) {
    const [px, py] = points[index - 1];
    const [x, y] = points[index];
    const [nx, ny] = points[index + 1];
    const before = Math.min(radius, Math.hypot(x - px, y - py) / 2);
    const after = Math.min(radius, Math.hypot(nx - x, ny - y) / 2);
    const bx = x - Math.sign(x - px) * before;
    const by = y - Math.sign(y - py) * before;
    const ax = x + Math.sign(nx - x) * after;
    const ay = y + Math.sign(ny - y) * after;
    d += ` L ${bx} ${by} Q ${x} ${y} ${ax} ${ay}`;
  }
  const last = points[points.length - 1];
  return `${d} L ${last[0]} ${last[1]}`;
}

type Box = { x: number; y: number; width: number; height: number };
type RoutePoint = [number, number];

function segmentCrossesNode(a: RoutePoint, b: RoutePoint, box: Box): boolean {
  const inset = 5;
  if (Math.abs(a[1] - b[1]) < 0.1) {
    const y = a[1];
    return y > box.y + inset && y < box.y + box.height - inset && Math.max(Math.min(a[0], b[0]), box.x + inset) < Math.min(Math.max(a[0], b[0]), box.x + box.width - inset);
  }
  if (Math.abs(a[0] - b[0]) < 0.1) {
    const x = a[0];
    return x > box.x + inset && x < box.x + box.width - inset && Math.max(Math.min(a[1], b[1]), box.y + inset) < Math.min(Math.max(a[1], b[1]), box.y + box.height - inset);
  }
  return true;
}

function routeAroundNodes(source: Box, target: Box, obstacles: Box[], sourceOffset = 0, targetOffset = 0, canvasWidth = CANVAS.width, canvasHeight = CANVAS.height): RoutePoint[] {
  const sxCenter = source.x + source.width / 2;
  const syCenter = source.y + source.height / 2;
  const txCenter = target.x + target.width / 2;
  const tyCenter = target.y + target.height / 2;
  const candidates: RoutePoint[][] = [];
  if (txCenter >= sxCenter) {
    const sx = source.x + source.width; const tx = target.x;
    const sy = Math.max(source.y + 12, Math.min(source.y + source.height - 12, syCenter + sourceOffset));
    const ty = Math.max(target.y + 12, Math.min(target.y + target.height - 12, tyCenter + targetOffset));
    for (let x = 20; x < canvasWidth; x += 20) candidates.push([[sx, sy], [x, sy], [x, ty], [tx, ty]]);
  } else {
    const sx = source.x; const tx = target.x + target.width;
    const sy = Math.max(source.y + 12, Math.min(source.y + source.height - 12, syCenter + sourceOffset));
    const ty = Math.max(target.y + 12, Math.min(target.y + target.height - 12, tyCenter + targetOffset));
    for (let x = 20; x < canvasWidth; x += 20) candidates.push([[sx, sy], [x, sy], [x, ty], [tx, ty]]);
  }
  const sourceX = Math.max(source.x + 12, Math.min(source.x + source.width - 12, sxCenter + sourceOffset));
  const targetX = Math.max(target.x + 12, Math.min(target.x + target.width - 12, txCenter + targetOffset));
  for (let y = 20; y < canvasHeight; y += 20) {
    candidates.push([[sourceX, source.y + source.height], [sourceX, y], [targetX, y], [targetX, target.y]]);
    candidates.push([[sourceX, source.y], [sourceX, y], [targetX, y], [targetX, target.y + target.height]]);
  }
  const scored = candidates.map((points) => {
    const segments = points.slice(1).map((point, index) => [points[index], point] as const);
    const collisions = segments.reduce((count, [a, b]) => count + obstacles.filter((box) => segmentCrossesNode(a, b, box)).length, 0);
    const length = segments.reduce((sum, [a, b]) => sum + Math.abs(a[0] - b[0]) + Math.abs(a[1] - b[1]), 0);
    return { points, collisions, length };
  });
  scored.sort((a, b) => a.collisions - b.collisions || a.length - b.length);
  return scored[0].points;
}


export type StreamCanvasProps = {
  groups: CanvasGroup[];
  edges: CanvasEdge[];
  defaultPositions: Record<string, Point>;
  positionKey: string;
  statusLabels: Record<string, string>;
  filters: CanvasFilter[];
  legend: Array<{ className: string; label: string }>;
  edgeKinds: string[];
  edgeWidth: (kind: string) => number;
  kicker: string;
  title: string;
  ariaLabel: string;
  renderDetailExtra?: (node: CanvasNode) => ReactNode;
  /** Group label positions; default: left edge, 45 px above the group's top node. */
  groupLabelPositions?: Record<string, Point>;
  /** Text of the link on a node and in the detail panel. */
  linkLabel?: string;
  detailLinkLabel?: string;
};

function loadPositions(key: string, defaults: Record<string, Point>): Record<string, Point> {
  try { return { ...defaults, ...JSON.parse(localStorage.getItem(key) || "{}") }; }
  catch { return defaults; }
}

export function StreamCanvas({
  groups, edges, defaultPositions, positionKey, statusLabels, filters, legend, edgeKinds, edgeWidth,
  kicker, title, ariaLabel, renderDetailExtra, groupLabelPositions,
  linkLabel = "Cấu hình", detailLinkLabel = "Mở cấu hình",
}: StreamCanvasProps) {
  const diagramRef = useRef<HTMLDivElement>(null);
  const viewportRef = useRef<HTMLDivElement>(null);
  const dragRef = useRef<{ type: "pan" | "node"; id?: string; x: number; y: number; originX: number; originY: number } | null>(null);
  const didDrag = useRef(false);
  const userZoomed = useRef(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [hoveredId, setHoveredId] = useState<string | null>(null);
  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const [positions, setPositions] = useState<Record<string, Point>>(() => loadPositions(positionKey, defaultPositions));
  useEffect(() => {
    // New nodes (a host added to the cluster) take their default slot.
    setPositions((value) => ({ ...defaultPositions, ...value }));
  }, [defaultPositions]);
  const pointOf = useCallback((id: string): Point => positions[id] || defaultPositions[id] || { x: 20, y: 20 }, [positions, defaultPositions]);
  const canvasSize = useMemo(() => {
    const placed = Object.values(positions);
    return {
      width: Math.min(4_360, Math.max(CANVAS.width, ...placed.map((point) => point.x + 360))),
      height: Math.min(4_220, Math.max(CANVAS.height, ...placed.map((point) => point.y + 220))),
    };
  }, [positions]);
  const [snapEnabled, setSnapEnabled] = useState(true);
  const [filterKey, setFilterKey] = useState(filters[0]?.key ?? "all");
  const filter = filters.find((candidate) => candidate.key === filterKey) || filters[0];
  const [collapsedGroups, setCollapsedGroups] = useState<Record<string, boolean>>({});
  const [paths, setPaths] = useState<EdgePath[]>([]);
  const allNodes = useMemo(() => groups.flatMap((group) => group.nodes), [groups]);
  const selected = allNodes.find((node) => node.id === selectedId) || null;
  const SelectedIcon = selected?.icon;
  const focusedId = hoveredId || selectedId;
  const groupLabels = useMemo(() => groups.map((group) => {
    const explicit = groupLabelPositions?.[group.id];
    if (explicit) return { group, left: explicit.x, top: explicit.y };
    const ys = group.nodes.map((node) => defaultPositions[node.id]?.y).filter((y): y is number => typeof y === "number");
    return { group, left: 20, top: Math.max(0, (ys.length ? Math.min(...ys) : 20) - 45) };
  }), [groups, defaultPositions, groupLabelPositions]);
  const visibleNodeIds = useMemo(() => {
    return new Set(allNodes.filter((node) => {
      const group = groups.find((candidate) => candidate.nodes.some((item) => item.id === node.id));
      if (group && collapsedGroups[group.id]) return false;
      return filter ? filter.node(node) : true;
    }).map((node) => node.id));
  }, [allNodes, filter, collapsedGroups, groups]);

  useEffect(() => {
    if (selectedId && !visibleNodeIds.has(selectedId)) setSelectedId(null);
  }, [selectedId, visibleNodeIds]);

  const recalculate = useCallback(() => {
    const diagram = diagramRef.current;
    const viewport = viewportRef.current;
    if (!diagram || !viewport) return;
    const diagramRect = diagram.getBoundingClientRect();
    const nodeBoxes = new Map<string, Box>();
    diagram.querySelectorAll<HTMLElement>("[data-node-id]").forEach((element) => {
      const rect = element.getBoundingClientRect();
      nodeBoxes.set(element.dataset.nodeId || "", {
        x: (rect.left - diagramRect.left) / zoom, y: (rect.top - diagramRect.top) / zoom,
        width: rect.width / zoom, height: rect.height / zoom,
      });
    });
    const edgeIndices = new Map<string, number>();
    const targetIndices = new Map<string, number>();
    const visibleEdges = edges.filter((edge) => visibleNodeIds.has(edge.from) && visibleNodeIds.has(edge.to) && (!filter || filter.edge(edge)));
    const sourceCounts = new Map<string, number>(); const targetCounts = new Map<string, number>();
    visibleEdges.forEach((edge) => { sourceCounts.set(edge.from, (sourceCounts.get(edge.from) || 0) + 1); targetCounts.set(edge.to, (targetCounts.get(edge.to) || 0) + 1); });
    const nextPaths = visibleEdges.flatMap((edge) => {
      const source = nodeBoxes.get(edge.from);
      const target = nodeBoxes.get(edge.to);
      if (!source || !target) return [];
      const index = edgeIndices.get(edge.from) || 0;
      edgeIndices.set(edge.from, index + 1);
      const targetIndex = targetIndices.get(edge.to) || 0;
      targetIndices.set(edge.to, targetIndex + 1);
      const obstacles = [...nodeBoxes.entries()].filter(([id]) => id !== edge.from && id !== edge.to).map(([, box]) => box);
      const sourceOffset = (index - ((sourceCounts.get(edge.from) || 1) - 1) / 2) * 14;
      const targetOffset = (targetIndex - ((targetCounts.get(edge.to) || 1) - 1) / 2) * 14;
      const route = routeAroundNodes(source, target, obstacles, sourceOffset, targetOffset, canvasSize.width, canvasSize.height);
      const [sx, sy] = route[0];
      const [tx, ty] = route[route.length - 1];
      const midpoint = route[Math.floor(route.length / 2)];
      return [{ ...edge, d: roundedOrthogonal(route, 8), sx, sy, tx, ty, mx: midpoint[0], my: midpoint[1] }];
    });
    setPaths(nextPaths);
  }, [edges, filter, visibleNodeIds, zoom, positions, canvasSize]);

  const fitToScreen = useCallback(() => {
    const viewport = viewportRef.current;
    const diagram = diagramRef.current;
    if (!viewport || !diagram) return;
    const width = Math.max(1, viewport.clientWidth - 24);
    const height = Math.max(1, viewport.clientHeight - 24);
    userZoomed.current = false;
    setZoom(Math.min(1, width / canvasSize.width, height / canvasSize.height));
    setPan({ x: 0, y: 0 });
  }, [canvasSize]);

  useLayoutEffect(() => {
    const frame = window.requestAnimationFrame(() => {
      recalculate();
      if (!userZoomed.current) fitToScreen();
    });
    const observer = new ResizeObserver(() => {
      recalculate();
      if (!userZoomed.current) fitToScreen();
    });
    if (diagramRef.current) observer.observe(diagramRef.current);
    if (viewportRef.current) observer.observe(viewportRef.current);
    return () => { window.cancelAnimationFrame(frame); observer.disconnect(); };
  }, [recalculate, fitToScreen]);

  useLayoutEffect(() => {
    const frame = window.requestAnimationFrame(recalculate);
    return () => window.cancelAnimationFrame(frame);
  }, [recalculate, zoom]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "0" || event.ctrlKey || event.altKey || event.metaKey) return;
      const target = event.target as HTMLElement | null;
      if (target?.closest("input, textarea, select, [contenteditable='true']")) return;
      fitToScreen();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [fitToScreen]);

  const changeZoom = (delta: number) => {
    userZoomed.current = true;
    setZoom((value) => Math.max(0.35, Math.min(1.5, value + delta)));
  };

  const onWheel = (event: WheelEvent<HTMLDivElement>) => {
    if (!event.ctrlKey) return;
    event.preventDefault();
    userZoomed.current = true;
    setZoom((value) => Math.max(0.35, Math.min(1.5, value + (event.deltaY < 0 ? 0.06 : -0.06))));
  };

  const onPointerDown = (event: PointerEvent<HTMLDivElement>) => {
    if (event.button !== 0 || (event.target as HTMLElement).closest("button, a")) return;
    const node = (event.target as HTMLElement).closest<HTMLElement>("[data-node-id]");
    if (node) {
      const id = node.dataset.nodeId || "";
      const point = pointOf(id);
      dragRef.current = { type: "node", id, x: event.clientX, y: event.clientY, originX: point.x, originY: point.y };
      userZoomed.current = true;
      didDrag.current = false;
      node.setPointerCapture(event.pointerId);
    } else {
      setSelectedId(null);
      dragRef.current = { type: "pan", x: event.clientX, y: event.clientY, originX: pan.x, originY: pan.y };
      event.currentTarget.setPointerCapture(event.pointerId);
      event.currentTarget.classList.add("is-panning");
    }
    event.preventDefault();
  };
  const onPointerMove = (event: PointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current;
    if (!drag) return;
    if (drag.type === "pan") setPan({ x: drag.originX + event.clientX - drag.x, y: drag.originY + event.clientY - drag.y });
    else if (drag.id) {
      if (Math.hypot(event.clientX - drag.x, event.clientY - drag.y) > 3) didDrag.current = true;
      const next = {
        x: Math.max(0, Math.min(4_000, drag.originX + (event.clientX - drag.x) / zoom)),
        y: Math.max(0, Math.min(4_000, drag.originY + (event.clientY - drag.y) / zoom)),
      };
      setPositions((value) => ({ ...value, [drag.id!]: next }));
    }
  };
  const onPointerUp = (event: PointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current;
    if (drag?.type === "node" && drag.id) {
      setPositions((value) => {
        const point = value[drag.id!] || defaultPositions[drag.id!];
        const next = { ...value, [drag.id!]: snapEnabled ? {
          x: Math.max(0, Math.round(point.x / 20) * 20),
          y: Math.max(0, Math.round(point.y / 20) * 20),
        } : point };
        try { localStorage.setItem(positionKey, JSON.stringify(next)); } catch { /* layout stays in memory */ }
        return next;
      });
    }
    dragRef.current = null;
    event.currentTarget.classList.remove("is-panning");
  };
  const resetLayout = () => {
    setPositions(defaultPositions);
    try { localStorage.removeItem(positionKey); } catch { /* nothing stored */ }
    setPan({ x: 0, y: 0 });
    fitToScreen();
  };
  const jumpToNode = (id: string) => { setSelectedId(id); const point = pointOf(id); setPan({ x: viewportRef.current!.clientWidth / 2 - (point.x + 100) * zoom, y: viewportRef.current!.clientHeight / 2 - (point.y + 40) * zoom }); };

  return (
      <section className="installation-stream__panel" aria-label={ariaLabel}>
        <div className="installation-stream__panel-head">
          <div><span className="installation-stream__section-kicker">{kicker}</span><h2>{title}</h2></div>
          <div className="installation-stream__zoom" aria-label="Điều khiển sơ đồ" title="Ctrl + cuộn để zoom · 0 để vừa màn hình">
            <button type="button" onClick={fitToScreen} aria-label="Vừa màn hình" title="Vừa màn hình (0)"><Maximize2 size={14} /> <span>Vừa màn hình</span></button>
            <button type="button" onClick={() => changeZoom(-0.1)} aria-label="Thu nhỏ" title="Thu nhỏ"><ZoomOut size={14} /></button>
            <span aria-live="polite">{Math.round(zoom * 100)}%</span>
            <button type="button" onClick={() => changeZoom(0.1)} aria-label="Phóng to" title="Phóng to"><ZoomIn size={14} /></button>
            <button type="button" className={snapEnabled ? "is-active" : ""} onClick={() => setSnapEnabled((value) => !value)} aria-pressed={snapEnabled} title="Bật/tắt bắt lưới 20px"><Magnet size={13} /> Bắt lưới</button>
            <button type="button" onClick={resetLayout} title="Khôi phục bố cục mặc định"><RotateCcw size={13} /> Khôi phục bố cục</button>
          </div>
        </div>
        <div className="installation-stream__legend">
          <div className="installation-stream__filters" role="group" aria-label="Lọc sơ đồ">
            {filters.map((candidate) => <button type="button" key={candidate.key} className={filterKey === candidate.key ? "is-active" : ""} aria-pressed={filterKey === candidate.key} onClick={() => setFilterKey(candidate.key)}>{candidate.label}</button>)}
          </div>
          {legend.map((item) => <span className="installation-stream__legend-line" key={item.label}><i className={item.className} /> {item.label}</span>)}
          <span className="installation-stream__keyboard-hint">Ctrl + cuộn: zoom · Kéo node: sắp xếp · Kéo nền: di chuyển · 0: vừa màn hình</span>
        </div>
        <div className="installation-stream__workspace">
          <div
            className="installation-stream__viewport"
            ref={viewportRef}
            onWheel={onWheel}
            onPointerDown={onPointerDown}
            onPointerMove={onPointerMove}
            onPointerUp={onPointerUp}
            onPointerCancel={onPointerUp}
            aria-label="Sơ đồ luồng; cuộn chuột thường để cuộn trang, Ctrl cộng cuộn để phóng to"
          >
            <div className="installation-stream__stage" style={{ width: `${canvasSize.width * zoom}px`, height: `${canvasSize.height * zoom}px` }}>
              <div className="installation-stream__diagram-transform" style={{ transform: `translate(${pan.x}px, ${pan.y}px) scale(${zoom})` }}>
                <div className="installation-stream__diagram" ref={diagramRef} style={{ width: canvasSize.width, height: canvasSize.height }}>
                  {groupLabels.map(({ group, left, top }) => <button type="button" key={group.id} className={`installation-stream__group-label${collapsedGroups[group.id] ? " is-collapsed" : ""}`} style={{ left, top }} aria-expanded={!collapsedGroups[group.id]} onClick={() => setCollapsedGroups((value) => ({ ...value, [group.id]: !value[group.id] }))}>{group.title} <small>{collapsedGroups[group.id] ? "đã thu gọn" : `${group.nodes.length} thành phần`}</small></button>)}
                  <svg className="installation-stream__edges" aria-label="Các kết nối giữa thành phần" width={canvasSize.width} height={canvasSize.height} viewBox={`0 0 ${canvasSize.width} ${canvasSize.height}`}>
                    <defs>
                      {edgeKinds.map((kind) => <marker key={kind} id={`stream-arrow-${kind}`} viewBox="0 0 10 10" refX="8" refY="5" markerWidth="10" markerHeight="10" orient="auto"><path d="M 0 0 L 10 5 L 0 10 z" className={`arrow-${kind}`} /></marker>)}
                    </defs>
                    {paths.map((edge, index) => {
                      const active = !focusedId || edge.from === focusedId || edge.to === focusedId;
                      const strokeWidth = edgeWidth(edge.kind);
                      return <g key={`${edge.from}-${edge.to}-${index}`} className={`installation-stream__edge-group is-${edge.kind}${active ? " is-highlighted" : focusedId ? " is-dimmed" : ""}`} onMouseEnter={() => setHoveredId(edge.from)} onMouseLeave={() => setHoveredId(null)}>
                        <title>{`${allNodes.find((node) => node.id === edge.from)?.title || edge.from} → ${allNodes.find((node) => node.id === edge.to)?.title || edge.to} · ${edge.label}`}</title>
                        <path d={edge.d} strokeWidth={strokeWidth} markerEnd={`url(#stream-arrow-${edge.kind})`} />
                        <circle cx={edge.sx} cy={edge.sy} r="2" />
                        {zoom >= 0.7 && <g className="installation-stream__edge-label"><rect x={edge.mx - Math.max(38, edge.label.length * 3.1)} y={edge.my - 11} width={Math.max(76, edge.label.length * 6.2)} height="18" rx="5" /><text x={edge.mx} y={edge.my + 2}>{edge.label}</text></g>}
                        <path className="installation-stream__edge-hit" d={edge.d} />
                      </g>;
                    })}
                  </svg>
                  {allNodes.filter((node) => visibleNodeIds.has(node.id)).map((node) => {
                            const Icon = node.icon;
                            const selectedNode = selectedId === node.id;
                            const featureStatus = statusLabels[node.status] || node.status;
                            const point = pointOf(node.id);
                            const connected = focusedId && edges.some((edge) => (edge.from === focusedId || edge.to === focusedId) && (edge.from === node.id || edge.to === node.id));
                            return (
                              <div
                                key={node.id}
                                className={`installation-stream__node is-${node.status}${selectedNode ? " is-selected" : ""}${dragRef.current?.id === node.id ? " is-dragging" : ""}${focusedId && focusedId !== node.id && !connected ? " is-dimmed" : ""}${connected ? " is-connected" : ""}`}
                                data-node-id={node.id}
                                style={{ left: point.x, top: point.y, zIndex: dragRef.current?.id === node.id ? 10 : selectedNode ? 3 : 2 }}
                                role="button"
                                tabIndex={0}
                                title={`${node.title}: ${featureStatus}`}
                                aria-label={`${node.title}: ${featureStatus}`}
                                aria-pressed={selectedNode}
                                onMouseEnter={() => setHoveredId(node.id)}
                                onMouseLeave={() => setHoveredId(null)}
                                onFocus={() => setHoveredId(node.id)}
                                onBlur={() => setHoveredId(null)}
                                onClick={() => { if (!didDrag.current) setSelectedId((value) => value === node.id ? null : node.id); }}
                                onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); setSelectedId(node.id); } }}
                              >
                                <span className="installation-stream__status-dot" aria-hidden="true" />
                                <span className="installation-stream__node-icon"><Icon size={17} /></span>
                                <span className="installation-stream__node-copy"><strong>{node.title}</strong><small>{node.subtitle}</small></span>
                                {node.configHref && <a className="installation-stream__configure" href={node.configHref} onClick={(event) => event.stopPropagation()}>{linkLabel} <span aria-hidden="true">→</span></a>}
                              </div>
                            );
                          })}
                </div>
              </div>
            </div>
            <div className="installation-stream__minimap" role="button" tabIndex={0} aria-label="Minimap, nhấn để di chuyển sơ đồ" onPointerDown={(event) => event.stopPropagation()} onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); fitToScreen(); } }} onClick={(event) => { const rect = event.currentTarget.getBoundingClientRect(); const x = ((event.clientX - rect.left) / rect.width) * canvasSize.width; const y = ((event.clientY - rect.top) / rect.height) * canvasSize.height; setPan({ x: viewportRef.current!.clientWidth / 2 - x * zoom, y: viewportRef.current!.clientHeight / 2 - y * zoom }); }}>
              <svg viewBox={`0 0 ${canvasSize.width} ${canvasSize.height}`}><rect width={canvasSize.width} height={canvasSize.height} className="minimap-bg" />
                {paths.map((edge, index) => <path key={index} d={edge.d} className={`minimap-edge is-${edge.kind}`} />)}
                {allNodes.filter((node) => visibleNodeIds.has(node.id)).map((node) => { const p = pointOf(node.id); return <rect key={node.id} x={p.x} y={p.y} width="200" height="82" rx="6" className={`minimap-node is-${node.status}`} />; })}
                <rect className="minimap-view" x={Math.max(0, -pan.x / zoom)} y={Math.max(0, -pan.y / zoom)} width={viewportRef.current ? viewportRef.current.clientWidth / zoom : 0} height={viewportRef.current ? viewportRef.current.clientHeight / zoom : 0} />
              </svg>
            </div>
          </div>

          <aside className="installation-stream__detail" aria-live="polite">
            {selected && SelectedIcon ? <>
              <div className={`installation-stream__detail-icon is-${selected.status}`}><SelectedIcon size={18} /></div>
              <div className="installation-stream__detail-title">{selected.title}<span className={`installation-stream__detail-status is-${selected.status}`}>{statusLabels[selected.status] || selected.status}</span></div>
              <p className="installation-stream__detail-subtitle">{selected.subtitle}</p>
              <ul>{selected.facts.map((fact) => <li key={fact}>{fact}</li>)}</ul>
              <section className="installation-stream__connections">
                <h3>Nhận dữ liệu từ</h3>
                {edges.filter((edge) => edge.to === selected.id).map((edge) => <button type="button" key={`${edge.from}-${edge.to}-${edge.label}`} onClick={() => jumpToNode(edge.from)}><span>{allNodes.find((node) => node.id === edge.from)?.title || edge.from}</span><small>{edge.label}</small></button>)}
                {!edges.some((edge) => edge.to === selected.id) && <small>Không có luồng vào được khai báo.</small>}
                <h3>Gửi dữ liệu tới</h3>
                {edges.filter((edge) => edge.from === selected.id).map((edge) => <button type="button" key={`${edge.from}-${edge.to}-${edge.label}`} onClick={() => jumpToNode(edge.to)}><span>{allNodes.find((node) => node.id === edge.to)?.title || edge.to}</span><small>{edge.label}</small></button>)}
                {!edges.some((edge) => edge.from === selected.id) && <small>Không có luồng ra được khai báo.</small>}
              </section>
              {selected.configHref && <a className="installation-stream__detail-link" href={selected.configHref}>{detailLinkLabel} <span aria-hidden="true">→</span></a>}
              {renderDetailExtra?.(selected)}
            </> : <div className="installation-stream__empty-detail"><Workflow size={25} /><strong>Chọn một thành phần</strong><span>Thông tin chi tiết sẽ hiển thị tại đây.</span></div>}
          </aside>
        </div>
      </section>
  );
}
