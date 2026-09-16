import { RefreshCw } from "lucide-react";

/** In-flow refresh banner. Never absolutely positioned: a floating one
 *  covered real content and hid whatever it overlapped. */
export function LoadingState({ message }: { message: string }) {
  return (
    <div className="state-banner state-banner--loading" role="status" aria-live="polite">
      <RefreshCw size={15} className="dashboard-spin" aria-hidden="true" />
      <span>{message}</span>
    </div>
  );
}
