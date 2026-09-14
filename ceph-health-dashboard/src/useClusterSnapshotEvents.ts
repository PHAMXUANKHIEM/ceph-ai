import { useEffect, useState } from "react";

type SnapshotEvent = {
  event?: string;
  cluster_id?: string;
  sections?: string[];
};

/**
 * Subscribe to cluster invalidation hints. HTTP remains the source of truth;
 * polling continues in the consumer as a fallback for proxies without WS.
 */
export function useClusterSnapshotEvents(clusterId: string): number {
  const [eventVersion, setEventVersion] = useState(0);

  useEffect(() => {
    if (!clusterId) return;
    let stopped = false;
    let socket: WebSocket | null = null;
    let reconnectTimer: number | null = null;
    let attempt = 0;

    const connect = () => {
      if (stopped || document.hidden) return;
      const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
      const url = `${protocol}//${window.location.host}/ws/cluster-state?cluster_id=${encodeURIComponent(clusterId)}`;
      socket = new WebSocket(url);
      socket.onopen = () => { attempt = 0; };
      socket.onmessage = (message) => {
        try {
          const payload = JSON.parse(message.data) as SnapshotEvent;
          if (payload.event && (!payload.cluster_id || payload.cluster_id === clusterId)) {
            setEventVersion((value) => value + 1);
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
      socket?.close();
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, [clusterId]);

  return eventVersion;
}
