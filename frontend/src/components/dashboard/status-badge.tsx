import type { VideoStatus } from "@/lib/api/videos";

export const STATUS_LABELS: Record<VideoStatus, string> = {
  uploaded: "Uploaded",
  processing: "Processing",
  ready: "Ready",
  failed: "Failed",
};

// The text label carries the meaning; the symbol and color only reinforce it.
const STATUS_STYLES: Record<VideoStatus, { symbol: string; className: string }> = {
  uploaded: { symbol: "○", className: "border-border text-fg" },
  processing: { symbol: "◐", className: "border-info text-fg" },
  ready: { symbol: "✓", className: "border-success text-fg" },
  failed: { symbol: "✕", className: "border-danger text-fg" },
};

/** A processing status as a visible text label, never as color alone. */
export function StatusBadge({ status }: { status: VideoStatus }) {
  const { symbol, className } = STATUS_STYLES[status];
  return (
    <span
      data-status={status}
      className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs font-medium ${className}`}
    >
      <span aria-hidden="true">{symbol}</span>
      <span className="sr-only">Status: </span>
      {STATUS_LABELS[status]}
    </span>
  );
}
