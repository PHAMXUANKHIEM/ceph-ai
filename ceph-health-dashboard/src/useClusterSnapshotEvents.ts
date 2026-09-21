import { useEffect, useRef, useState } from "react";

export type SnapshotEvent = {
  event?: string;
  cluster_id?: string;
  sections?: string[];
  action_id?: string;
  action_status?: string;
  action_state?: "queued" | "running" | "verifying" | "succeeded" | "failed" | "rejected";
  request_id?: string;
};

function requestId(): string {
  const cryptoApi = window.crypto as Crypto & { randomUUID?: () => string };
  return cryptoApi.randomUUID?.() || `browser-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

/**
 * Subscribe to cluster invalidation hints. HTTP remains the source of truth;
 * polling continues in the consumer as a fallback for proxies without WS.
 */
export function useClusterSnapshotEvents(
  clusterId: string,
  onEvent?: (event: SnapshotEvent) => void,
): number {
  const [eventVersion, setEventVersion] = useState(0);
  const onEventRef = useRef(onEvent);
  onEventRef.current = onEvent;

  useEffect(() => {
    if (!clusterId) return;
    let stopped = false;
    let socket: WebSocket | null = null;
    let reconnectTimer: number | null = null;
    let coalesceTimer: number | null = null;
    let attempt = 0;

    const connect = () => {
      if (stopped || document.hidden) return;
      const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
      const url = `${protocol}//${window.location.host}/ws/cluster-state?cluster_id=${encodeURIComponent(clusterId)}&request_id=${encodeURIComponent(requestId())}&reconnect=${attempt > 0 ? "1" : "0"}`;
      socket = new WebSocket(url);
      socket.onopen = () => { attempt = 0; };
      socket.onmessage = (message) => {
        try {
          const payload = JSON.parse(message.data) as SnapshotEvent;
          if (payload.event && (!payload.cluster_id || payload.cluster_id === clusterId)) {
            onEventRef.current?.(payload);
            // A collector can publish several section changes in one refresh.
            // Coalesce them briefly so one commit does not trigger one HTTP
            // read per event/tab while preserving the latest invalidation.
            if (coalesceTimer === null) {
              coalesceTimer = window.setTimeout(() => {
                coalesceTimer = null;
                if (!stopped) setEventVersion((value) => value + 1);
              }, 150);
            }
          }
        } catch {
          // Ignore malformed hints; the normal HTTP fallback remains active.
        }
      };
      socket.onclose = () => {
        socket = null;
        if (stopped || document.hidden) return;
        const delay = Math.min(30_000, 1_000 * 2 ** Math.min(attempt++, 5));
        reconnectTimer = window.setTimeout(connect, delay);
      };
      socket.onerror = () => socket?.close();
    };

    const onVisibilityChange = () => {
      if (document.hidden) {
        socket?.close();
        socket = null;
      } else if (!socket) {
        connect();
      }
    };

    connect();
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      stopped = true;
      if (reconnectTimer !== null) window.clearTimeout(reconnectTimer);
      if (coalesceTimer !== null) window.clearTimeout(coalesceTimer);
      socket?.close();
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, [clusterId]);

  return eventVersion;
}
