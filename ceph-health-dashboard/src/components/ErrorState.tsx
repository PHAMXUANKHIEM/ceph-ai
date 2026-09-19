import type { ReactNode } from "react";

type ErrorStateProps = {
  message: ReactNode;
  /** `error` is a failure the operator must act on and is announced as an
   *  alert; `warning` covers degraded-but-usable states such as a stale
   *  snapshot, which must not interrupt a screen reader mid-sentence. */
  tone?: "error" | "warning";
  onRetry?: () => void;
  retryLabel?: string;
  retryDisabled?: boolean;
  onDismiss?: () => void;
};

export function ErrorState({
  message, tone = "error", onRetry, retryLabel = "Thử lại", retryDisabled = false, onDismiss,
}: ErrorStateProps) {
  return (
    <div
      className={`state-banner state-banner--${tone}`}
      role={tone === "error" ? "alert" : "status"}
      aria-live="polite"
    >
      <span className="state-banner__message">{message}</span>
      {onRetry && (
        <button type="button" onClick={onRetry} disabled={retryDisabled}>
          {retryLabel}
        </button>
      )}
      {onDismiss && (
        <button type="button" className="state-banner__dismiss" onClick={onDismiss} aria-label="Ẩn cảnh báo">
          ×
        </button>
      )}
    </div>
  );
}
