/** Media timestamps: display and parsing of positions in a video, in seconds. */

const PART = /^\d+$/;
const SECONDS_PART = /^\d+(\.\d+)?$/;

/** "0:05", "12:34", "1:02:03" (rounded down to whole seconds); a missing time is "--:--". */
export function formatTimestamp(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds) || seconds < 0) {
    return "--:--";
  }
  const total = Math.floor(seconds);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = String(total % 60).padStart(2, "0");
  return hours > 0 ? `${hours}:${String(minutes).padStart(2, "0")}:${secs}` : `${minutes}:${secs}`;
}

/** A clip length such as "32.5 s" (at most one decimal). */
export function formatSeconds(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) {
    return "Unknown";
  }
  return `${Math.round(seconds * 10) / 10} s`;
}

/**
 * Parses "90", "90.5", "1:30", "1:30.25" or "1:02:03" into seconds. Minutes and
 * seconds after the first part must be below 60. Returns `null` for anything
 * else, including blank input.
 */
export function parseTimestamp(input: string): number | null {
  const value = input.trim();
  if (!value) {
    return null;
  }
  const parts = value.split(":");
  if (parts.length > 3) {
    return null;
  }
  const last = parts[parts.length - 1];
  const leading = parts.slice(0, -1);
  if (!SECONDS_PART.test(last) || !leading.every((part) => PART.test(part))) {
    return null;
  }
  const seconds = Number(last);
  if (leading.length > 0 && seconds >= 60) {
    return null;
  }
  if (leading.length === 2 && Number(leading[1]) >= 60) {
    return null;
  }
  const total = leading.reduce((sum, part) => sum * 60 + Number(part) * 60, 0) + seconds;
  return Number.isFinite(total) ? total : null;
}
