export type SnapshotState = "loading" | "refreshing" | "fresh" | "stale" | "error" | "unknown";

type SnapshotMeta = {
  available?: boolean;
  collected_at?: string | null;
  stale?: boolean;
  refreshing?: boolean;
};

export function getSnapshotState(
  meta: SnapshotMeta | null | undefined,
  options: { error?: unknown; hasData?: boolean } = {},
): SnapshotState {
  if (options.error) return "error";
  if (!options.hasData && !meta?.collected_at && !meta?.available) return "loading";
  if (meta?.refreshing) return "refreshing";
  if (meta?.stale) return "stale";
  if (meta?.available === false || !meta?.collected_at) return "unknown";
  return "fresh";
}

export const SNAPSHOT_STATE_LABEL: Record<SnapshotState, string> = {
  loading: "Đang tải",
  refreshing: "Đang đồng bộ",
  fresh: "Mới",
  stale: "Dữ liệu cũ",
  error: "Lỗi đồng bộ",
  unknown: "Chưa có dữ liệu",
};
