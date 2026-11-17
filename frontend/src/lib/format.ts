/** Display helpers for video sizes, durations and dates. */

const SIZE_UNITS = ["B", "KB", "MB", "GB", "TB"] as const;

/** "512 B", "1.5 MB", "2 GB" (binary units, like the backend's limits). */
export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) {
    return "Unknown";
  }
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < SIZE_UNITS.length - 1) {
    value /= 1024;
    unit += 1;
  }
  const rounded = unit === 0 ? value : Math.round(value * 10) / 10;
  return `${rounded} ${SIZE_UNITS[unit]}`;
}

/** "0:42", "12:05", "1:02:03"; `null` (not probed yet) is "Unknown". */
export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds) || seconds < 0) {
    return "Unknown";
  }
  const total = Math.round(seconds);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = String(total % 60).padStart(2, "0");
  return hours > 0 ? `${hours}:${String(minutes).padStart(2, "0")}:${secs}` : `${minutes}:${secs}`;
}

/** A spoken-friendly duration for totals: "2 h 5 min", "3 min", "0 min". */
export function formatTotalDuration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) {
    return "0 min";
  }
  const minutes = Math.round(seconds / 60);
  const hours = Math.floor(minutes / 60);
  return hours > 0 ? `${hours} h ${minutes % 60} min` : `${minutes} min`;
}

/** A medium date in the user's locale; an unparseable value is returned as is. */
export function formatDate(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) {
    return iso;
  }
  return date.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}
