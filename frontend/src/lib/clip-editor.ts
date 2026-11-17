/**
 * Pure helpers of the clip editor.
 *
 * Two time bases are used and never mixed:
 * - **Source time**: seconds of the uploaded video. Clip boundaries
 *   (`start_seconds`, `end_seconds`) and the player's `currentTime` use it.
 * - **Clip time**: seconds from the clip start. Captions use it, as does the
 *   render pipeline (the ASS file is written for the cut clip, so its
 *   0:00:00.00 is the clip's first frame). Clip time = source time - clip start.
 *
 * Caption timing follows `app/captions/ass.py`: times are rounded to
 * centiseconds, a caption lasts at least 1 cs, and libass shows an event while
 * `start <= now < end`. Text is cleaned, upper-cased and wrapped as there.
 */

import type { ClipLimits } from "@/lib/api/clips";
import type { Caption, CaptionInput } from "@/lib/api/captions";
import { formatSeconds, formatTimestamp, parseTimestamp } from "@/lib/timestamps";

/** Nudge steps of the boundary controls, in seconds. */
export const NUDGE_STEPS = [0.5, 1] as const;

/** Floating-point slack the backend allows on lengths and the caption end (`TIME_TOLERANCE_SECONDS`). */
export const TIME_TOLERANCE_SECONDS = 0.001;

/** Longest caption text the backend accepts (`CAPTION_TEXT_MAX_LENGTH`). */
export const CAPTION_TEXT_MAX_LENGTH = 1000;

/** Most captions per clip (`MAX_CAPTIONS_PER_CLIP`). */
export const MAX_CAPTIONS = 600;

/** Length of a caption added with "Add caption", when the clip has room for it. */
export const NEW_CAPTION_SECONDS = 2;

/** Rounds to milliseconds, the precision the backend stores. */
export function roundMs(seconds: number): number {
  return Math.round(seconds * 1000) / 1000;
}

/** The value shown in a seconds input ("12.5"). */
export function formatInputSeconds(seconds: number): string {
  return String(roundMs(seconds));
}

/** Seconds with up to two decimals, for the playhead display ("12.35 s"). */
export function formatPreciseSeconds(seconds: number): string {
  return Number.isFinite(seconds) ? `${(Math.round(seconds * 100) / 100).toFixed(2)} s` : "--";
}

// Timing (mirrors app/captions/ass.py)

/** `_to_centiseconds`: rounded half up, never negative. */
export function toCentiseconds(seconds: number): number {
  return Math.max(0, Math.floor(seconds * 100 + 0.5));
}

export interface TimedText {
  start_seconds: number;
  end_seconds: number;
  text: string;
}

/**
 * The captions the rendered clip shows at `clipTime` (seconds from the clip
 * start), in start order. Uses the ASS event times: start and end rounded to
 * centiseconds, at least 1 cs long, shown while `start <= now < end`.
 * Captions whose text is empty after cleaning are never rendered.
 */
export function captionsAt<T extends TimedText>(captions: readonly T[], clipTime: number): T[] {
  if (!Number.isFinite(clipTime) || clipTime < 0) {
    return [];
  }
  // In milliseconds, like libass; the epsilon absorbs float noise such as 1.5 * 1000 = 1499.9999.
  const now = clipTime * 1000 + 1e-6;
  return captions
    .filter((caption) => Number.isFinite(caption.start_seconds) && Number.isFinite(caption.end_seconds))
    .map((caption) => {
      const start = toCentiseconds(caption.start_seconds);
      return { caption, start, end: Math.max(toCentiseconds(caption.end_seconds), start + 1) };
    })
    .filter(({ caption, start, end }) => start * 10 <= now && now < end * 10 && cleanCaptionLines(caption.text).length > 0)
    .sort((a, b) => a.start - b.start)
    .map(({ caption }) => caption);
}

/** Source-video time to clip time. */
export function toClipTime(sourceSeconds: number, clipStart: number): number {
  return sourceSeconds - clipStart;
}

// Text (mirrors app/captions/generate.py)

// Unicode category C (control, format, ...), as `unicodedata.category(...).startswith("C")`.
// Built at runtime because the compile target predates property escapes.
const CONTROL_CHARACTERS = new RegExp("\\p{C}", "gu");

/** The non-empty lines of `text`, with control characters replaced by spaces and whitespace collapsed. */
export function cleanCaptionLines(text: string): string[] {
  return text
    .replace(/\r\n?/g, "\n")
    .split("\n")
    .map((line) => line.replace(CONTROL_CHARACTERS, " ").split(/\s+/).filter(Boolean).join(" "))
    .filter(Boolean);
}

/**
 * Wraps every line at word boundaries to `maxChars` (Python's
 * `textwrap.wrap` with `break_long_words=True`): words longer than the limit
 * are broken, so no line is ever longer.
 */
export function wrapCaptionText(text: string, maxChars: number): string[] {
  const width = Math.max(1, Math.floor(maxChars));
  const wrapped: string[] = [];
  for (const line of cleanCaptionLines(text)) {
    let current = "";
    for (const original of line.split(" ")) {
      let word = original;
      while (word) {
        if (!current) {
          current = word.slice(0, width);
          word = word.slice(width);
          if (word) {
            wrapped.push(current);
            current = "";
          }
        } else if (current.length + 1 + word.length <= width) {
          current = `${current} ${word}`;
          word = "";
        } else if (word.length > width && width - current.length - 1 > 0) {
          const room = width - current.length - 1;
          wrapped.push(`${current} ${word.slice(0, room)}`);
          current = "";
          word = word.slice(room);
        } else {
          wrapped.push(current);
          current = "";
        }
      }
    }
    if (current) {
      wrapped.push(current);
    }
  }
  return wrapped;
}

/** The lines the render shows for a caption: upper-cased if the style says so, then wrapped. */
export function renderedCaptionLines(text: string, maxChars: number, uppercase: boolean): string[] {
  return wrapCaptionText(uppercase ? text.toUpperCase() : text, maxChars);
}

// Boundaries

/**
 * Moves a boundary by `delta` seconds. Rule: the result is clamped to the
 * video (`[0, duration]`, or `>= 0` while the duration is unknown) and
 * rounded to milliseconds. Order and length are not fixed here; they are
 * reported by `validateBoundaries` and block saving.
 */
export function nudgeTime(value: number, delta: number, durationSeconds: number | null): number {
  const max = durationSeconds !== null && durationSeconds > 0 ? durationSeconds : Number.POSITIVE_INFINITY;
  return roundMs(Math.min(Math.max(0, value + delta), max));
}

export interface BoundaryErrors {
  start?: string;
  end?: string;
  /** The length is outside the allowed limits. */
  length?: string;
}

export interface ClipBounds {
  start: number;
  end: number;
}

/**
 * Checks the boundary inputs (source-video seconds, or `m:ss`) with the
 * backend's rules: `0 <= start < end <= duration`, and a length within the
 * clip limits (1 ms tolerance). Without limits only the backend checks the
 * length.
 */
export function validateBoundaries(
  startInput: string,
  endInput: string,
  durationSeconds: number | null,
  limits: ClipLimits | null,
): { bounds: ClipBounds | null; errors: BoundaryErrors } {
  const errors: BoundaryErrors = {};
  const parsedStart = parseTimestamp(startInput);
  const parsedEnd = parseTimestamp(endInput);
  if (parsedStart === null) {
    errors.start = "Enter the start in seconds, such as 12.5.";
  }
  if (parsedEnd === null) {
    errors.end = "Enter the end in seconds, such as 42.5.";
  }
  if (parsedStart === null || parsedEnd === null) {
    return { bounds: null, errors };
  }
  const start = roundMs(parsedStart);
  const end = roundMs(parsedEnd);
  if (durationSeconds !== null && start >= durationSeconds) {
    errors.start = `The start must be before the end of the video (${formatTimestamp(durationSeconds)}).`;
  }
  if (end <= start) {
    errors.end = "The end must be after the start.";
  } else if (durationSeconds !== null && end > durationSeconds) {
    errors.end = `The end must be within the video (${formatTimestamp(durationSeconds)}).`;
  }
  if (!errors.start && !errors.end && limits) {
    const length = end - start;
    if (
      length < limits.min_seconds - TIME_TOLERANCE_SECONDS ||
      length > limits.max_seconds + TIME_TOLERANCE_SECONDS
    ) {
      errors.length = `A clip must be between ${limits.min_seconds} and ${limits.max_seconds} seconds long; this one is ${formatSeconds(length)}.`;
    }
  }
  if (errors.start || errors.end || errors.length) {
    return { bounds: null, errors };
  }
  return { bounds: { start, end }, errors };
}

/** Whether the inputs differ from the saved range (invalid input always counts as a change). */
export function boundariesChanged(startInput: string, endInput: string, saved: ClipBounds): boolean {
  const start = parseTimestamp(startInput);
  const end = parseTimestamp(endInput);
  return (
    start === null || end === null || roundMs(start) !== roundMs(saved.start) || roundMs(end) !== roundMs(saved.end)
  );
}

// Captions

/** A caption being edited; times are clip-time inputs. */
export interface CaptionDraft {
  /** Stable React key. */
  key: string;
  start: string;
  end: string;
  text: string;
}

export interface CaptionDraftErrors {
  start?: string;
  end?: string;
  text?: string;
}

export function draftsFromCaptions(captions: readonly Caption[]): CaptionDraft[] {
  return captions.map((caption) => ({
    key: caption.id,
    start: formatInputSeconds(caption.start_seconds),
    end: formatInputSeconds(caption.end_seconds),
    text: caption.text,
  }));
}

function signatureTime(input: string): string {
  const parsed = parseTimestamp(input);
  return parsed === null ? `raw:${input}` : String(roundMs(parsed));
}

/** A value that is equal for two draft lists exactly when they would save the same captions. */
export function draftsSignature(drafts: readonly CaptionDraft[]): string {
  return JSON.stringify(drafts.map((draft) => [signatureTime(draft.start), signatureTime(draft.end), draft.text]));
}

/**
 * A new caption after the last one: from the last end (or 0) for
 * NEW_CAPTION_SECONDS, shortened to the clip. Returns `null` when the clip has
 * no room left.
 */
export function nextCaptionTimes(
  drafts: readonly CaptionDraft[],
  clipLength: number | null,
): { start: number; end: number } | null {
  let start = 0;
  for (const draft of drafts) {
    const end = parseTimestamp(draft.end);
    if (end !== null) {
      start = Math.max(start, roundMs(end));
    }
  }
  let end = roundMs(start + NEW_CAPTION_SECONDS);
  if (clipLength !== null) {
    end = Math.min(end, roundMs(clipLength));
  }
  return end - start >= 0.01 ? { start, end } : null;
}

/**
 * Checks the drafts with the rules of `PUT /clips/{id}/captions`: text that is
 * not blank once cleaned (at most 1000 characters, every line at most
 * `maxCharsPerLine`), times rounded to milliseconds with
 * `0 <= start < end <= clip length` (1 ms tolerance at the end), and captions
 * in time order without overlapping (touching is allowed), and at most
 * MAX_CAPTIONS of them (reported in `listError`). Returns the payload only
 * when every caption is valid.
 */
export function validateCaptionDrafts(
  drafts: readonly CaptionDraft[],
  clipLength: number | null,
  maxCharsPerLine: number,
): { captions: CaptionInput[] | null; errors: CaptionDraftErrors[]; listError?: string } {
  const errors: CaptionDraftErrors[] = [];
  const captions: CaptionInput[] = [];
  let previousEnd: number | null = null;
  const length = clipLength === null ? null : roundMs(clipLength);

  drafts.forEach((draft, index) => {
    const rowErrors: CaptionDraftErrors = {};
    const lines = cleanCaptionLines(draft.text);
    const text = lines.join("\n");
    if (!text) {
      rowErrors.text = "Enter the caption text.";
    } else if (text.length > CAPTION_TEXT_MAX_LENGTH) {
      rowErrors.text = `Caption text must be at most ${CAPTION_TEXT_MAX_LENGTH} characters.`;
    } else {
      const longLine = lines.findIndex((line) => line.length > maxCharsPerLine);
      if (longLine !== -1) {
        rowErrors.text = `Each line must be at most ${maxCharsPerLine} characters; line ${longLine + 1} has ${lines[longLine].length}. Press Enter to break it.`;
      }
    }

    const parsedStart = parseTimestamp(draft.start);
    const parsedEnd = parseTimestamp(draft.end);
    if (parsedStart === null) {
      rowErrors.start = "Enter the start in seconds from the clip start.";
    }
    if (parsedEnd === null) {
      rowErrors.end = "Enter the end in seconds from the clip start.";
    }
    const start = parsedStart === null ? null : roundMs(parsedStart);
    let end = parsedEnd === null ? null : roundMs(parsedEnd);
    if (start !== null && end !== null) {
      if (length !== null && end > length && end - length <= TIME_TOLERANCE_SECONDS) {
        end = length;
      }
      if (end <= start) {
        rowErrors.end = "The caption must end after it starts.";
      } else if (length !== null && (start >= length || end > length)) {
        rowErrors.end = `Captions must lie within the clip (0 to ${formatInputSeconds(length)} s).`;
      } else if (previousEnd !== null && start < previousEnd) {
        rowErrors.start = `Captions must not overlap: start at or after ${formatInputSeconds(previousEnd)} s, where caption ${index} ends.`;
      }
    }
    if (start !== null && end !== null && end > start) {
      previousEnd = previousEnd === null ? end : Math.max(previousEnd, end);
    }
    if (!rowErrors.start && !rowErrors.end && !rowErrors.text && start !== null && end !== null) {
      captions.push({ start_seconds: start, end_seconds: end, text });
    }
    errors.push(rowErrors);
  });

  if (drafts.length > MAX_CAPTIONS) {
    return {
      captions: null,
      errors,
      listError: `A clip can have at most ${MAX_CAPTIONS} captions; remove ${drafts.length - MAX_CAPTIONS}.`,
    };
  }
  return { captions: captions.length === drafts.length ? captions : null, errors };
}

/**
 * The caption a backend error refers to (0-based), if any: `details.index` of
 * the caption errors, or the list index in the `loc` of a `validation_error`.
 */
export function captionErrorIndex(details: unknown): number | null {
  if (typeof details === "object" && details !== null && !Array.isArray(details)) {
    const index = (details as { index?: unknown }).index;
    return typeof index === "number" && Number.isInteger(index) && index >= 0 ? index : null;
  }
  if (Array.isArray(details)) {
    for (const item of details) {
      const loc = typeof item === "object" && item !== null ? (item as { loc?: unknown }).loc : null;
      if (Array.isArray(loc)) {
        const at = loc.indexOf("captions");
        const index = at === -1 ? undefined : loc[at + 1];
        if (typeof index === "number" && Number.isInteger(index) && index >= 0) {
          return index;
        }
      }
    }
  }
  return null;
}
